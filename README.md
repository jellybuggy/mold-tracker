# 外贸项目追踪系统

帮助注塑模具厂追踪项目进度、管理邮件、自动提醒待处理事项。

## 在线访问

前端已部署到 GitHub Pages：
https://jellybuggy.github.io/mold-tracker/

## 技术架构

- **前端**：静态 HTML + Firebase Firestore（云端数据）
- **后端**：Python（邮件同步 + 提醒）
- **托管**：GitHub Pages（免费）

## 本地开发

### 1. 配置 Firebase

1. 进入 [Firebase Console](https://console.firebase.google.com)
2. 选择你的项目 → 项目设置 → 服务账号
3. 点击「生成新的私钥」，下载 JSON 文件
4. 重命名为 `firebase-service-account.json`，放到项目根目录

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

复制 `config.json.example` 为 `config.json`，填写：
- 邮箱地址和密码
- 飞书 Webhook（可选）
- 项目文件夹路径

### 4. 运行后端

```bash
python app.py
```

后端功能：
- 扫描本地文件夹，创建/更新 Firebase 项目数据
- 同步邮件到 Firebase
- 检查待确认事项，超时触发三种提醒

### 5. 访问前端

浏览器打开 https://jellybuggy.github.io/mold-tracker/

## 工作流程

```
报价 → 确认 → 收模具首款 → 开模 → 样品 → 收模具费余款 → 收产品首款 → 量产 → 发货 → 收尾款
```

## 文件夹命名格式

```
[阶段]-[材料]-[产品名]-[客户简称]-[日期]
示例：报价-PA66-壳体-KUNZ-20260526
```

## 提醒系统

待确认事项超1天未回复，同时触发：
1. **Windows 弹窗** - 电脑上弹出通知
2. **飞书** - 发送到飞书群/手机
3. **邮件** - 发送到 service@hxpmold.com

## 邮件关键词

在项目详情页设置德语关键词，用于自动匹配客户邮件。

## 依赖

```
flask
requests
firebase-admin
win10toast
apscheduler
```