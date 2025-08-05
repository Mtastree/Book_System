# 📚 图书馆智能推荐与感悟分享平台

这是一个基于微信公众号的交互式Web应用，旨在为图书馆读者提供个性化的图书推荐服务，并构建一个围绕书籍的感悟分享社区。

## ✨ 项目特色

- **微信生态集成**：深度集成微信公众号，支持菜单交互、消息回复和网页授权，提供无缝的用户体验。
- **个性化图书推荐**：分析用户的借阅历史，智能推荐可能感兴趣的书籍，并为无借阅历史的用户提供热门推荐。
- **动态感悟社区**：用户可以对图书馆藏书或任何自己读过的书发布感悟，形成一个活跃的读书交流平台。
- **丰富的社交互动**：支持对感悟进行点赞、评论（未来功能）和删除，增强用户间的互动。
- **完善的用户系统**：用户可通过微信快速绑定读者证，并自定义个人昵称和主页。
- **响应式网页设计**：所有Web页面均采用Bootstrap 5构建，完美适配手机和桌面浏览器。
- **管理员功能**：内置简单的内容管理机制，管理员可对不当言论进行隐藏（软删除）。

## 🚀 主要功能

- **微信公众号交互**
  - [x] 扫码关注与自动欢迎。
  - [x] 关键词回复（绑定、解绑、推荐、帮助）。
  - [x] 自定义菜单，一键直达核心功能。
- **用户系统**
  - [x] 微信一键登录（网页授权）。
  - [x] 绑定/解绑读者证。
  - [x] 自定义昵称。
  - [x] 个人主页，展示个人信息和已发布的感悟。
  - [x] 管理（编辑/删除）自己的感悟。
- **图书与感悟**
  - [x] 图书列表展示，支持按书名搜索和分页。
  - [x] 书籍详情页，包含真实封面、详细信息和相关感悟。
  - [x] 感悟广场，集中展示所有用户的感悟，支持分页。
  - [x] 发布感悟，支持对馆藏书和用户自定义书籍发布。
  - [x] 对感悟进行点赞。
- **管理功能**
  - [x] 管理员身份识别。
  - [x] 对全站任意感悟进行隐藏（软删除）。

## 🛠️ 技术栈

- **后端**: Python, Flask
- **数据库**: MySQL
- **微信开发**: 微信公众平台接口, `requests`
- **前端**: HTML5, CSS3, JavaScript, Bootstrap 5, jQuery (用于AJAX)
- **环境管理**: `python-dotenv`
- **数据处理**: `pandas` (用于初始化数据)
- **定时任务**: `apscheduler` (用于定时推送)

## 部署与运行

### 1. 环境准备

- Python 3.8+
- MySQL 5.7+
- 一个已认证的微信公众号（服务号）
- 一个内网穿透工具（如 Natapp, Ngrok）用于本地开发调试

### 2. 安装依赖

克隆项目到本地后，在项目根目录下创建一个虚拟环境，并安装所有必要的库：

```bash
# 创建虚拟环境 (可选但推荐)
python -m venv .venv
# 激活虚拟环境
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```
*(你需要手动创建一个 `requirements.txt` 文件，内容如下)*

**requirements.txt:**
```
Flask
requests
mysql-connector-python
python-dotenv
pandas
openpyxl
apscheduler
gunicorn  # 推荐用于生产环境部署
```

### 3. 数据库设置

1.  在你的 MySQL 服务器上创建一个新的数据库（例如 `library_db`）。
2.  执行项目中的 SQL 文件（如 `schema.sql`，你需要手动创建）来创建所有必需的表 (`books`, `readers`, `reflections`, `likes`, `user_books` 等)。
3.  使用提供的数据初始化脚本（如 `init_data.py`）将书籍信息导入到 `books` 表中。

### 4. 配置环境变量

在项目根目录下创建一个 `.env` 文件，并填入以下配置信息。**此文件不应提交到版本控制系统。**

```env
# 微信公众号配置
WECHAT_TOKEN=your_wechat_token
WECHAT_APPID=your_wechat_appid
WECHAT_SECRET=your_wechat_secret

# 数据库配置
DB_HOST=localhost
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_DATABASE=library_db

# Flask 应用配置
SECRET_KEY=a_very_long_and_random_secret_string # 必须设置，用于session加密
DEBUG=True # 开发时设为 True，生产环境设为 False

# 菜单创建开关
CREATE_MENU=False # 首次运行或需要更新菜单时设为 True
```

### 5. 微信公众号后台配置

1.  **服务器配置**:
    - 登录微信公众平台 -> 基本配置 -> 服务器配置。
    - **URL**: `http://<你的公网域名>/wechat`
    - **Token**: 与 `.env` 文件中的 `WECHAT_TOKEN` 保持一致。
    - **EncodingAESKey**: 随机生成。
    - **消息加解密方式**: 选择“安全模式”或“明文模式”（与代码对应）。
2.  **网页授权域名**:
    - 公众号设置 -> 功能设置 -> 网页授权域名。
    - 填入你的公网域名（**不带 `http://`**）。
3.  **IP白名单**:
    - 基本配置 -> 公众号开发信息 -> IP白名单。
    - 填入你服务器的公网 IP 地址。

### 6. 运行应用

1.  **启动内网穿透**：
    ```bash
    # 示例: natapp -authtoken=... http 80
    natapp -authtoken=<你的natapp_token> http <你的Flask端口>
    ```
2.  **启动 Flask 应用**：
    ```bash
    python app.py
    ```
    *或者使用 Gunicorn 在生产环境运行：*
    ```bash
    gunicorn -w 4 -b 0.0.0.0:80 app:app
    ```

## 未来展望

- [ ] **评论/回复系统**：构建完整的楼中楼评论功能。
- [ ] **消息通知**：实现站内信和微信模板消息通知。
- [ ] **AJAX化核心操作**：提升发布、删除、点赞等操作的流畅度。
- [ ] **缓存机制**：为高频访问的页面引入缓存，提升性能。
- [ ] **标签系统**：允许用户为感悟打标签，增强内容发现。
- [ ] **热门/精选推荐**：算法驱动的优质内容推荐。
