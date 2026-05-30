#!/usr/bin/env python3
"""
外贸项目追踪系统 - 后端服务
功能：文件夹扫描、邮件同步、自动提醒、阶段推进
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify
import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import firebase_admin
from firebase_admin import credentials, firestore
from apscheduler.schedulers.background import BackgroundScheduler
try:
    from win10toast import ToastNotifier
    WIN10TOAST_OK = True
except ImportError:
    WIN10TOAST_OK = False

# ========== Flask API ==========
api_app = Flask(__name__)

@api_app.route('/api/projects', methods=['GET'])
def api_get_projects():
    """获取所有项目列表"""
    try:
        db = init_firebase()
        docs = db.collection('projects').get()
        result = []
        for doc in docs:
            d = doc.to_dict()
            d['id'] = doc.id
            # 计算下一步
            next_info = get_next_action(d)
            d['next_action'] = next_info['action']
            d['next_stage'] = next_info['next_stage']
            result.append(d)
        return jsonify({'success': True, 'projects': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@api_app.route('/api/project/<project_id>', methods=['GET'])
def api_get_project(project_id):
    """获取单个项目详情"""
    try:
        db = init_firebase()
        doc = db.collection('projects').document(project_id).get()
        if not doc.exists:
            return jsonify({'success': False, 'error': '项目不存在'}), 404
        d = doc.to_dict()
        d['id'] = doc.id
        next_info = get_next_action(d)
        d['next_action'] = next_info['action']
        d['next_stage'] = next_info['next_stage']
        return jsonify({'success': True, 'project': d})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@api_app.route('/api/project/<project_id>/manual', methods=['POST'])
def api_add_manual_entry(project_id):
    """手动添加电话/WhatsApp/备注记录"""
    try:
        data = request.json
        entry_type = data.get('type', 'note')  # 'call', 'whatsapp', 'note'
        content = data.get('content', '')
        date_str = data.get('date', '')

        if not content:
            return jsonify({'success': False, 'error': '内容不能为空'}), 400

        db = init_firebase()
        success = add_manual_entry(db, project_id, entry_type, content, date_str)

        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': '项目不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@api_app.route('/api/sync', methods=['POST'])
def api_sync():
    """手动触发一次同步"""
    try:
        config = load_config()
        db = init_firebase()
        sync_all(config, db)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ========== 配置 ==========
CONFIG_PATH = Path(__file__).parent / 'config.json'

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ========== 阶段推进规则 ==========
# 邮件类型 → 触发推进到哪个阶段
# 规则：从当前阶段出发，遇到匹配的邮件类型就推进
STAGE_ADVANCE_RULES = {
    # 项目立项
    '立项': ['projekt', 'project start', '立项', '项目启动', 'new project', 'project initialization'],
    # 收到报价 Angebot → 进入报价阶段
    '报价': ['angebot', 'preis', 'quote', 'quotation', 'pricing', 'kosten', '成本', '报价', '价格', 'offer'],
    # 收到订单 Bestellung → 进入确认阶段
    '确认': ['bestellung', 'order', 'auftrag', 'purchase order', 'po ', '订单', '订购', 'bestellt', 'confirmed order'],
    # 收到模具首款付款 → 进入收模具首款阶段
    '收模具首款': ['werkzeug', 'werkzeugkosten', 'tooling', 'tooling cost', 'mold cost', '模具首款', '模具费首款', 'muster werkzeug'],
    # 开模（模具制作中）→ 进入开模阶段
    '开模': ['werkzeug', 'formenbau', 'mold making', '模具制作', '模具开工', 'mold in progress', 'werkzeug in arbeit'],
    # 收到样品 → 进入样品阶段
    '样品': ['muster', 'sample', 'probe', 'mustern', '样件', '样品', '样板', 'prototype'],
    # 样品确认 → 样品通过，可以收模具费余款
    '样品确认': ['musterbestätigung', 'sample approval', 'sample confirm', '样件确认', '样品确认', 'mustern bestätigt', 'sample approved', 'muster genehmigt'],
    # 收到模具费余款付款 → 进入收模具费余款阶段
    '收模具费余款': ['werkzeug rest', 'tooling balance', '模具余款', '模具尾款', 'rest payment tooling'],
    # 收到产品首款付款 → 进入收产品首款阶段
    '收产品首款': ['artikel首个', 'produkt首个', 'product first', '首批货款', '产品首款', 'first production payment'],
    # 量产确认 → 进入量产阶段
    '量产': ['massenproduktion', 'mass production', 'mass production start', '量产', '开始量产', 'production started'],
    # 发货 → 进入发货阶段
    '发货': ['versand', 'lieferung', 'shipment', 'shipping', '发货', '发运', 'dispatch', 'liefern'],
    # 收到尾款付款 → 进入收尾款阶段
    '收尾款': ['rest', 'final payment', 'end payment', '尾款', '最后款项', 'final balance', 'remaining payment'],
}

# 阶段顺序
STAGES = ['立项', '报价', '确认', '收模具首款', '开模', '样品', '收模具费余款', '收产品首款', '量产', '发货', '收尾款']

def get_stage_from_email(email_type, subject, body):
    """
    根据邮件内容判断应该推进到哪个阶段
    返回：阶段名称 或 None（不触发阶段推进）
    """
    text = f"{subject} {body}".lower()

    for stage, keywords in STAGE_ADVANCE_RULES.items():
        for kw in keywords:
            if kw.lower() in text:
                return stage

    return None


def advance_project_stage(project_data, emails):
    """
    根据项目邮件自动推进阶段
    只进不退：从当前阶段出发，找到第一封能推进的邮件就停在那个阶段
    返回：(新阶段, 触发推进的邮件主题)
    """
    current_stage = project_data.get('stage', '立项')
    current_idx = STAGES.index(current_stage) if current_stage in STAGES else 0

    # 按时间顺序扫描邮件（最早的在前，最新的在后）
    sorted_emails = sorted(emails, key=lambda x: x.get('date', ''))

    new_stage = current_stage
    trigger_email = None

    for email in sorted_emails:
        subject = email.get('subject', '')
        body = email.get('body', '')
        email_type = email.get('email_type', '')

        # 检查是否触发阶段推进
        advanced_stage = get_stage_from_email(email_type, subject, body)

        if advanced_stage and advanced_stage in STAGES:
            advanced_idx = STAGES.index(advanced_stage)

            # 只进不退：只有在比当前阶段更后面的情况下才推进
            if advanced_idx > current_idx:
                new_stage = advanced_stage
                trigger_email = subject
                current_idx = advanced_idx  # 更新当前位置

    return new_stage, trigger_email


def advance_all_projects(db, projects):
    """批量更新所有项目的阶段"""
    updated_count = 0

    for proj in projects:
        if not proj.get('emails'):
            continue

        doc_id = proj.get('folder_name', proj['name'])
        doc = db.collection('projects').document(doc_id).get()

        if not doc.exists:
            continue

        data = doc.to_dict()
        old_stage = data.get('stage', '报价')
        new_stage, trigger_email = advance_project_stage(data, proj.get('emails', []))

        if new_stage != old_stage:
            db.collection('projects').document(doc_id).update({
                'stage': new_stage,
                'stage_updated': datetime.now().isoformat(),
                'stage_trigger_email': trigger_email
            })
            logger.info(f"项目 {proj['name']} 阶段更新: {old_stage} → {new_stage} (触发: {trigger_email})")
            updated_count += 1

    return updated_count


def get_next_action(project_data):
    """
    根据当前阶段返回下一步应该做什么
    返回：下一步动作描述
    """
    current_stage = project_data.get('stage', '立项')
    current_idx = STAGES.index(current_stage) if current_stage in STAGES else 0

    next_stage = STAGES[current_idx + 1] if current_idx < len(STAGES) - 1 else None

    if not next_stage:
        return {'action': '项目已完成所有阶段', 'next_stage': None}

    next_actions = {
        '立项': '收集客户信息，填写项目基本信息，准备报价材料',
        '报价': '等待客户确认报价 (Angebot bestätigen)',
        '确认': '等待客户下订单 (Bestellung)',
        '收模具首款': '等待客户支付模具首款 (Werkzeug Anzahlung)',
        '开模': '模具制作中，确保交期',
        '样品': '等待客户确认样品 (Muster bestätigen)',
        '收模具费余款': '等待客户支付模具费余款 (Werkzeug Rest)',
        '收产品首款': '等待客户支付产品首款 (Produkt Anzahlung)',
        '量产': '准备量产，确保交期',
        '发货': '安排发货 (Versand)',
        '收尾款': '等待客户支付尾款 (Restzahlung)',
    }

    return {
        'action': next_actions.get(current_stage, '继续跟踪'),
        'next_stage': next_stage
    }

# ========== Firebase 初始化 ==========
def init_firebase():
    """初始化 Firebase 连接"""
    cred = credentials.Certificate('firebase-service-account.json')
    firebase_admin.initialize_app(cred)
    return firestore.client()


# ========== 配置加载 ==========
def load_config():
    """从 config.json 加载配置"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ========== 文件夹扫描 ==========
def scan_projects_folder(folder_path):
    """
    扫描项目文件夹，解析文件夹命名格式
    格式：[阶段]-[材料]-[产品名]-[客户简称]-[日期]
    """
    projects = []
    folder = Path(folder_path)

    if not folder.exists():
        logger.error(f"文件夹不存在: {folder_path}")
        return projects

    for entry in folder.iterdir():
        if not entry.is_dir():
            continue

        name = entry.name
        parts = name.split('-')

        if len(parts) < 5:
            logger.warning(f"文件夹命名格式不符: {name}")
            continue

        # 解析：阶段-材料-产品名-客户简称-日期
        stage = parts[0]
        material = parts[1]
        product = '-'.join(parts[2:-2])  # 可能包含中划线的产品名
        customer = parts[-2]
        date_str = parts[-1]

        # 验证阶段是否合法
        if stage not in STAGES:
            logger.warning(f"未知的阶段 '{stage}' in {name}")
            continue

        # 验证日期格式
        try:
            datetime.strptime(date_str, '%Y%m%d')
        except ValueError:
            logger.warning(f"日期格式错误 '{date_str}' in {name}")
            continue

        projects.append({
            'name': product,
            'stage': stage,
            'material': material,
            'customer': customer,
            'folder_name': name,
            'created': date_str,
            'folder_path': str(entry)
        })

    return projects


# ========== 邮件同步 ==========
def connect_imap(config):
    """连接 IMAP 服务器"""
    try:
        mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
        mail.login(config['email'], config['email_password'])
        mail.select('INBOX')
        return mail
    except Exception as e:
        logger.error(f"IMAP 连接失败: {e}")
        return None


def decode_header_str(header):
    """解码邮件头部"""
    if not header:
        return ''
    decoded_parts = decode_header(header)
    if isinstance(decoded_parts, tuple):
        return decoded_parts[0] or ''
    result = ''
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(charset or 'utf-8', errors='ignore')
        else:
            result += part
    return result


# ========== 邮件分类 ==========
# 德语/英语关键词匹配不同邮件类型
EMAIL_TYPE_PATTERNS = {
    '报价': {
        'keywords': ['angebot', 'preis', 'quote', 'quotation', 'pricing', 'kosten', '成本', '报价', '价格'],
        'weight': 1.0
    },
    '样品': {
        'keywords': ['muster', 'sample', 'probe', 'mustern', '样件', '样品', '样板'],
        'weight': 1.0
    },
    '样品确认': {
        'keywords': ['musterbestätigung', 'sample approval', 'sample confirm', '样件确认', '样品确认', 'mustern bestätigt'],
        'weight': 1.2
    },
    '订单': {
        'keywords': ['bestellung', 'order', 'auftrag', '订单', '订购', 'bestellt'],
        'weight': 1.0
    },
    '确认合同': {
        'keywords': ['bestätigung', 'confirm', 'vertrag', 'contract', 'confirmed', '合同确认', '确认', '确认书'],
        'weight': 1.0
    },
    '发货': {
        'keywords': ['versand', 'lieferung', 'shipment', 'shipping', 'delivery', '发货', '发运', 'liefern'],
        'weight': 1.0
    },
    '发货通知': {
        'keywords': ['versandbenachrichtigung', 'versandinfo', 'shipping notice', 'dispatch note', '发货通知', '发货信息'],
        'weight': 1.3
    },
    '付款': {
        'keywords': ['zahlung', 'payment', 'bezahlen', 'rechnung', 'invoice', '账单', '付款', '支付', 'zahlung für'],
        'weight': 1.0
    },
    '收款确认': {
        'keywords': ['zahlung erhalten', 'payment received', 'bezahlt', 'paid', '收款', '已付款', '付款确认'],
        'weight': 1.2
    },
    '发票': {
        'keywords': ['rechnung', 'invoice', 'factura', '发票', 'invoice'],
        'weight': 1.0
    },
    '技术问题': {
        'keywords': ['technisch', 'technical', 'problem', 'frage', 'issue', '问题', '技术问题', '质量'],
        'weight': 1.0
    },
    '投诉': {
        'keywords': ['reklamation', 'beschwerde', 'complaint', 'claim', '抱怨', '投诉', '索赔'],
        'weight': 1.5
    },
    '模具进度': {
        'keywords': ['werkzeug', 'tooling', 'formenbau', 'mold', '模具', '开模', '模具制作'],
        'weight': 1.0
    },
    '一般沟通': {
        'keywords': ['hallo', 'hello', 'hi', 'dear', 'freundlich', 'greeting', '您好', '问候', '沟通'],
        'weight': 0.3
    }
}

# 优先级：同类关键词出现越多，分类越准确
def classify_email(subject, body, sender=''):
    """
    根据邮件主题和正文分类邮件
    返回: (邮件类型, 置信度, 匹配关键词列表)
    """
    text = f"{subject} {body}".lower()
    scores = {}

    for email_type, type_info in EMAIL_TYPE_PATTERNS.items():
        score = 0
        matched_kws = []

        for kw in type_info['keywords']:
            if kw.lower() in text:
                score += type_info['weight']
                matched_kws.append(kw)

        if score > 0:
            scores[email_type] = {
                'score': score,
                'matched_keywords': matched_kws
            }

    if not scores:
        return ('一般沟通', 0.0, [])

    # 取最高分的类型
    best_type = max(scores.items(), key=lambda x: x[1]['score'])
    return (best_type[0], best_type[1]['score'], best_type[1]['matched_keywords'])


def extract_prices(text):
    """从文本中提取价格（欧元）"""
    prices = []
    # 匹配 €123,45 或 €123.45 或 123,45€ 格式
    patterns = [
        r'€\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)',
        r'(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*€',
        r'(?:EUR|euro)\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            # 标准化格式：统一使用句点作为小数点
            normalized = m.replace(',', '.')
            try:
                price_val = float(normalized)
                if 0 < price_val < 1000000:  # 合理价格范围
                    prices.append(round(price_val, 2))
            except ValueError:
                continue
    return list(set(prices))  # 去重


def extract_email_content(msg):
    """提取邮件的完整内容（主题、发件人、正文、附件列表）"""
    subject = decode_header_str(msg['Subject'])
    sender = decode_header_str(msg['From'])
    date_str = decode_header_str(msg['Date'])

    body = ''
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition', ''))

            # 获取附件文件名
            if 'attachment' in content_disposition:
                filename = decode_header_str(part.get_filename('filename'))
                if filename:
                    attachments.append(filename)

            # 提取文本内容（优先 plain text）
            if content_type == 'text/plain' and not body:
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    body = part.get_payload(decode=True).decode(charset, errors='ignore')
                except:
                    continue
            elif content_type == 'text/html' and not body:
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    body = part.get_payload(decode=True).decode(charset, errors='ignore')
                except:
                    continue
    else:
        try:
            charset = msg.get_content_charset() or 'utf-8'
            body = msg.get_payload(decode=True).decode(charset, errors='ignore')
        except:
            body = ''

    # 清理 HTML 标签（如果只有 HTML）
    if '<' in body and not attachments:
        import re
        body = re.sub(r'<[^>]+>', ' ', body)  # 简单去除 HTML 标签
        body = re.sub(r'\s+', ' ', body)  # 合并空白

    return subject, sender, date_str, body.strip(), attachments


def fetch_emails(mail, config, since_days=30):
    """获取最近 N 天的邮件，包含完整内容和分类"""
    emails = []
    try:
        # 搜索最近 N 天的邮件
        since_date = (datetime.now() - timedelta(days=since_days)).strftime('%d-%b-%Y')
        status, messages = mail.search(None, f'SINCE {since_date}')

        if status != 'OK':
            logger.warning("邮件搜索失败")
            return emails

        mail_ids = messages[0].split()
        logger.info(f"找到 {len(mail_ids)} 封近期邮件")

        for mail_id in mail_ids:
            try:
                status, msg_data = mail.fetch(mail_id, '(RFC822)')
                if status != 'OK':
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # 提取完整内容
                subject, sender, date_str, body, attachments = extract_email_content(msg)

                # 分类邮件
                email_type, confidence, matched_kws = classify_email(subject, body, sender)

                # 提取价格
                prices = extract_prices(body)

                # 提取邮件正文摘要（用于列表显示）
                body_preview = body[:800] if len(body) > 800 else body
                body_preview = body_preview.replace('\r\n', ' ').replace('\n', ' ').strip()

                emails.append({
                    'subject': subject,
                    'sender': sender,
                    'date': date_str,
                    'body': body,  # 完整正文
                    'body_preview': body_preview,  # 预览摘要
                    'prices': prices,
                    'email_type': email_type,  # 新增：邮件类型分类
                    'type_confidence': confidence,  # 新增：分类置信度
                    'matched_keywords': matched_kws,  # 新增：匹配的关键词
                    'attachments': attachments,  # 新增：附件列表
                    'raw_date': datetime.now().isoformat()
                })

            except Exception as e:
                logger.warning(f"解析邮件失败: {e}")
                continue

    except Exception as e:
        logger.error(f"获取邮件失败: {e}")

    return emails


def match_emails_to_projects(emails, projects):
    """根据关键词匹配邮件到项目"""
    for proj in projects:
        proj_emails = []
        keywords = proj.get('keywords', [])

        if not keywords:
            # 如果没有关键词，尝试用项目名和客户名匹配
            keywords = [proj['name'], proj['customer']]

        for em in emails:
            matched = False
            # 检查主题和正文
            text_to_search = f"{em['subject']} {em['body']}".lower()

            for kw in keywords:
                if kw.lower() in text_to_search:
                    matched = True
                    break

            # 额外检查：如果发件人包含客户名
            if not matched and proj['customer'].lower() in em['sender'].lower():
                matched = True

            if matched:
                proj_emails.append(em)

        proj['emails'] = proj_emails

    return projects


# ========== Firebase 同步 ==========
def sync_projects_to_firebase(db, projects):
    """同步项目数据到 Firebase"""
    for proj in projects:
        doc_id = proj.get('folder_name', proj['name'])

        # 查询是否已存在
        existing = db.collection('projects').document(doc_id).get()

        project_data = {
            'name': proj['name'],
            'stage': proj['stage'],
            'material': proj['material'],
            'customer': proj['customer'],
            'created': proj['created'],
            'updated': datetime.now().isoformat(),
        }

        if existing.exists:
            # 更新时保留 emails、pending_items 等
            existing_data = existing.to_dict()
            project_data['emails'] = existing_data.get('emails', [])
            project_data['pending_items'] = existing_data.get('pending_items', [])
            project_data['keywords'] = existing_data.get('keywords', [])
            project_data['price_history'] = existing_data.get('price_history', [])
        else:
            project_data['emails'] = []
            project_data['pending_items'] = []
            project_data['keywords'] = []
            project_data['price_history'] = []

        # 添加新邮件到历史
        new_emails = proj.get('emails', [])
        existing_emails = project_data.get('emails', [])
        existing_subjects = {e['subject'] for e in existing_emails}

        for new_em in new_emails:
            if new_em['subject'] not in existing_subjects:
                existing_emails.append(new_em)

        project_data['emails'] = existing_emails

        # 更新价格历史
        all_prices = project_data.get('price_history', [])
        for new_em in new_emails:
            for price in new_em.get('prices', []):
                all_prices.append({
                    'date': new_em['date'],
                    'price': price,
                    'source': new_em['subject'][:50]
                })
        project_data['price_history'] = all_prices[-50:]  # 保留最近50条

        db.collection('projects').document(doc_id).set(project_data)
        logger.info(f"同步项目: {doc_id}")

    return len(projects)


# ========== 提醒系统 ==========
def check_pending_reminders(db, config):
    """检查待确认事项超期并发送提醒"""
    reminder_days = config.get('reminder_days', 1)
    cutoff_date = (datetime.now() - timedelta(days=reminder_days)).strftime('%Y-%m-%d')

    try:
        docs = db.collection('projects').get()

        for doc in docs:
            data = doc.to_dict()
            pending_items = data.get('pending_items', [])

            for item in pending_items:
                if item.get('resolved'):
                    continue

                created = item.get('created', '')
                if created and created < cutoff_date:
                    project_name = data.get('name', '未知项目')
                    content = item.get('content', '')
                    send_reminders(project_name, content, config)

    except Exception as e:
        logger.error(f"检查提醒失败: {e}")


def send_reminders(project_name, content, config):
    """发送三种提醒"""
    message = f"【待确认事项超期】\n项目: {project_name}\n内容: {content}"

    # 1. Windows 弹窗
    if WIN10TOAST_OK:
        try:
            toaster = ToastNotifier()
            toaster.show_toast("外贸项目追踪系统", message, duration=10)
            logger.info(f"Windows 弹窗已发送: {project_name}")
        except Exception as e:
            logger.warning(f"Windows 弹窗失败: {e}")

    # 2. 飞书 Webhook
    feishu_webhook = config.get('feishu_webhook', '')
    if feishu_webhook and feishu_webhook != 'YOUR_FEISHU_WEBHOOK_URL':
        try:
            payload = {
                "msg_type": "text",
                "content": {"text": message}
            }
            resp = requests.post(feishu_webhook, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"飞书提醒已发送: {project_name}")
            else:
                logger.warning(f"飞书提醒失败: {resp.status_code}")
        except Exception as e:
            logger.warning(f"飞书提醒异常: {e}")

    # 3. 邮件提醒
    try:
        send_email(config, message)
        logger.info(f"邮件提醒已发送: {project_name}")
    except Exception as e:
        logger.warning(f"邮件提醒失败: {e}")


def send_email(config, message):
    """发送邮件提醒"""
    smtp_server = config.get('smtp_server', '')
    smtp_port = config.get('smtp_port', 465)
    email_addr = config.get('email', '')
    email_pass = config.get('email_password', '')

    if not smtp_server or not email_addr:
        return

    msg = MIMEMultipart()
    msg['From'] = email_addr
    msg['To'] = email_addr
    msg['Subject'] = f"【提醒】外贸项目待确认事项 - {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(message, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(email_addr, email_pass)
            server.send_message(msg)
    except Exception as e:
        # 尝试普通 SMTP
        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(email_addr, email_pass)
                server.send_message(msg)
        except Exception as e2:
            logger.warning(f"SMTP 发送失败: {e2}")


# ========== 主流程 ==========
def sync_all(config, db):
    """执行完整的同步流程"""
    logger.info("=" * 50)
    logger.info("开始同步...")

    # 1. 扫描文件夹
    projects_folder = config.get('projects_folder', '')
    projects = scan_projects_folder(projects_folder)
    logger.info(f"扫描到 {len(projects)} 个项目")

    # 2. 同步邮件
    mail = connect_imap(config)
    if mail:
        emails = fetch_emails(mail, config, since_days=30)
        logger.info(f"获取到 {len(emails)} 封邮件")

        # 3. 匹配邮件到项目
        projects = match_emails_to_projects(emails, projects)

        # 4. 同步到 Firebase
        sync_count = sync_projects_to_firebase(db, projects)
        logger.info(f"同步了 {sync_count} 个项目到 Firebase")

        # 4.5 根据邮件推进项目阶段
        updated = advance_all_projects(db, projects)
        logger.info(f"阶段自动推进了 {updated} 个项目")

        mail.logout()
    else:
        # 无邮件连接时仍同步项目数据
        for proj in projects:
            proj['emails'] = []
        sync_projects_to_firebase(db, projects)

    # 5. 检查提醒
    check_pending_reminders(db, config)

    logger.info("同步完成")
    logger.info("=" * 50)


def add_manual_entry(db, project_id, entry_type, content, date_str):
    """
    手动添加一条记录（电话/WhatsApp/备注等）
    entry_type: 'call', 'whatsapp', 'note'
    """
    entry = {
        'id': str(int(datetime.now().timestamp() * 1000)),
        'type': entry_type,
        'content': content,
        'date': date_str or datetime.now().strftime('%Y-%m-%d'),
        'created': datetime.now().isoformat()
    }

    doc = db.collection('projects').document(project_id).get()
    if not doc.exists:
        return False

    data = doc.to_dict()
    manual_entries = data.get('manual_entries', [])
    manual_entries.append(entry)

    db.collection('projects').document(project_id).update({
        'manual_entries': manual_entries
    })

    # 同时更新阶段（手动记录也可能触发阶段推进）
    emails = data.get('emails', [])
    new_stage, trigger = advance_project_stage(data, emails)
    if new_stage != data.get('stage'):
        db.collection('projects').document(project_id).update({
            'stage': new_stage,
            'stage_updated': datetime.now().isoformat(),
            'stage_trigger': trigger
        })

    return True


def main():
    """主入口"""
    logger.info("外贸项目追踪系统后端启动")

    # 加载配置
    config = load_config()
    logger.info(f"配置加载: 邮箱 {config['email']}")

    # 初始化 Firebase
    db = init_firebase()
    logger.info("Firebase 连接成功")

    # 立即执行一次同步
    sync_all(config, db)

    # 设置定时任务
    scheduler = BackgroundScheduler()
    # 每 30 分钟同步一次
    scheduler.add_job(sync_all, 'interval', minutes=30, args=[config, db])
    scheduler.start()
    logger.info("定时任务已启动 (每30分钟同步一次)")

    # 启动 Flask API（供前端手动上传等使用）
    logger.info("API 服务启动在 http://localhost:5000")
    api_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()