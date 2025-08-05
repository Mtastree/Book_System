import requests
from flask import Flask, request, make_response, render_template, redirect, url_for, jsonify
import hashlib
import time
import mysql.connector
import random
import re
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from mysql.connector import Error
from dotenv import load_dotenv
from get_reading_history import get_reading_history
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import json
from flask import session

# 加载环境变量
load_dotenv()

app = Flask(__name__)

# ====================== 配置信息 ======================
# 从环境变量获取配置
WECHAT_TOKEN = os.getenv('WECHAT_TOKEN', '')
# 微信公众号凭证
WECHAT_APPID = os.getenv('WECHAT_APPID', '')
WECHAT_SECRET = os.getenv('WECHAT_SECRET', '')
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', ''),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_DATABASE', 'library_db')
}
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://yourdomain.com/wechat_redirect')
CREATE_MENU = True
DEBUG = True
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'a_default_very_secret_key_that_should_be_changed')
# ====================== 图书推荐系统核心 ======================
class CallNumberParser:
    def __init__(self):
        self.class_set = self.build_class_set()

    def build_class_set(self):
        class_set = set()
        class_set.update(chr(i) for i in range(65, 91))

        t_subclasses = ['TB', 'TD', 'TE', 'TF', 'TG', 'TH', 'TJ', 'TK',
                        'TL', 'TM', 'TN', 'TP', 'TQ', 'TS', 'TU', 'TV']
        class_set.update(t_subclasses)

        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            class_set.update(f"{letter}{i}" for i in range(10))

        return class_set

    def parse_callno(self, callno):
        if not callno or not isinstance(callno, str):
            return None, None

        parts = callno.split('\\')
        class_part = parts[0].strip() if parts else callno
        clean_class = ''.join(c for c in class_part if c.isalnum())

        if not clean_class:
            return None, None

        if clean_class.startswith('T') and len(clean_class) >= 2:
            if clean_class[1].isalpha():
                subclass = clean_class[:2]
                if subclass in self.class_set:
                    return 'T', subclass

        for length in range(min(4, len(clean_class)), 0, -1):
            prefix = clean_class[:length]
            if prefix in self.class_set:
                return prefix[0], prefix

        return clean_class[0], clean_class[0]


class ReadingHistoryProcessor:
    def __init__(self, parser):
        self.parser = parser

    def process_history(self, history_items):
        seen_books = set()
        class_freq = defaultdict(int)
        subclass_freq = defaultdict(int)
        reader_id = None

        for item in history_items:
            callno = item.get("callNo", "")
            seen_books.add(callno)

            if reader_id is None:
                reader_id = item.get("readerId", "")

            main_class, subclass = self.parser.parse_callno(callno)
            if not main_class:
                continue

            class_freq[main_class] += 1
            subclass_freq[(main_class, subclass)] += 1

        return reader_id, seen_books, dict(class_freq), dict(subclass_freq)


class BookRecommender:
    def __init__(self, db_config):
        self.db_config = db_config
        self.parser = CallNumberParser()
        self.history_processor = ReadingHistoryProcessor(self.parser)
        self.recommend_history = {}  # 缓存推荐历史 {reader_id: [推荐记录]}

    def get_recommendations(self, reader_id, history_items, top_n=4):
        """
        获取推荐书籍
        :param reader_id: 读者ID
        :param history_items: 历史记录项列表
        :param top_n: 推荐数量
        :return: 推荐书籍列表
        """
        # 获取已推荐书籍（避免重复推荐）
        recommended_callnos = self.get_recommended_callnos(reader_id)

        # 处理阅读历史
        _, seen_books, class_freq, subclass_freq = self.history_processor.process_history(history_items)

        # 排除已推荐书籍
        seen_books.update(recommended_callnos)

        # 无历史记录时的兜底推荐
        if not class_freq:
            app.logger.info(f"读者 {reader_id} 无阅读历史，随机推荐书籍")
            recommendations = self.get_random_books(top_n, seen_books)
            if recommendations:
                self.save_recommendation(reader_id, recommendations)
            return recommendations, None

        # 按频率排序分类
        sorted_subclasses = sorted(subclass_freq.items(), key=lambda x: x[1], reverse=True)

        # 生成推荐
        recommendations = []

        # 按子类推荐高频书籍（3本）
        for (main_class, subclass), _ in sorted_subclasses:
            if len(recommendations) >= top_n - 1:  # 留1个位置给随机推荐
                break

            books = self.get_books_by_class(main_class, subclass, seen_books)
            if books:
                n = min(1, top_n - len(recommendations))  # 每个子类推荐1本
                selected = random.sample(books, n)
                recommendations.extend(selected)
                seen_books.update(b['索书号'] for b in selected)

        # 最后一本随机推荐（1本）
        if len(recommendations) < top_n:
            fallback = self.get_random_books(top_n - len(recommendations), seen_books)
            recommendations.extend(fallback)

        # 保存推荐记录
        if recommendations:
            self.save_recommendation(reader_id, recommendations)

        return recommendations, None

    def create_db_connection(self):
        try:
            conn = mysql.connector.connect(
                host=self.db_config['host'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                database=self.db_config['database'],
                charset='utf8mb4',
                collation='utf8mb4_unicode_ci'
            )
            return conn
        except Error as e:
            app.logger.error(f"数据库连接错误: {e}")
            return None

    def get_books_by_class(self, main_class, subclass, exclude_callnos):
        conn = self.create_db_connection()
        if not conn:
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            exclude_list = ', '.join(['%s'] * len(exclude_callnos)) if exclude_callnos else "''"
            like_pattern = f"{subclass}%"

            cursor.execute(f"""
                SELECT * FROM books
                WHERE 索书号 NOT IN ({exclude_list})
                AND 索书号 LIKE %s
                ORDER BY RAND()
                LIMIT 10
            """, tuple(exclude_callnos) + (like_pattern,))

            return cursor.fetchall()
        except Error as e:
            app.logger.error(f"查询书籍错误: {e}")
            return []
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def get_random_books(self, top_n=4, exclude_callnos=None):
        if exclude_callnos is None:
            exclude_callnos = set()

        conn = self.create_db_connection()
        if not conn:
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            exclude_list = ', '.join(['%s'] * len(exclude_callnos)) if exclude_callnos else "''"

            cursor.execute(f"""
                SELECT * FROM books
                WHERE 索书号 NOT IN ({exclude_list})
                ORDER BY RAND()
                LIMIT %s
            """, tuple(exclude_callnos) + (top_n,))

            return cursor.fetchall()
        except Error as e:
            app.logger.error(f"随机推荐错误: {e}")
            return []
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def get_recommended_callnos(self, reader_id):
        conn = self.create_db_connection()
        if not conn:
            return set()

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT book_call_no
                FROM recommend_history
                WHERE reader_id = %s
            """, (reader_id,))

            return {row[0] for row in cursor.fetchall()}
        except Error as e:
            app.logger.error(f"获取推荐历史错误: {e}")
            return set()
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def save_recommendation(self, reader_id, recommendations):
        if not recommendations:
            return

        conn = self.create_db_connection()
        if not conn:
            return

        try:
            cursor = conn.cursor()

            for book in recommendations:
                cursor.execute("""
                    INSERT INTO recommend_history
                    (reader_id, book_call_no, book_title, book_author, book_publisher, book_isbn, recommend_time)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """, (
                    reader_id,
                    book.get("索书号", ""),
                    book.get("题名", "未知书名"),
                    book.get("责任者", "未知作者"),
                    book.get("出版社", "未知出版社"),
                    book.get("标准号", "")
                ))

            # 清理旧记录
            cursor.execute("""
                DELETE FROM recommend_history
                WHERE id NOT IN (
                    SELECT id
                    FROM (
                        SELECT id
                        FROM recommend_history
                        WHERE reader_id = %s
                        ORDER BY recommend_time DESC
                        LIMIT 20
                    ) AS temp
                )
                AND reader_id = %s
            """, (reader_id, reader_id))

            conn.commit()
            app.logger.info(f"已为读者 {reader_id} 保存 {len(recommendations)} 条推荐记录")

        except Error as e:
            app.logger.error(f"保存推荐记录错误: {e}")
            conn.rollback()
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()


# ====================== 数据库操作 ======================
def get_db_connection():
    """创建并返回数据库连接"""
    try:
        conn = mysql.connector.connect(
            host=DB_CONFIG['host'],
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password'],
            database=DB_CONFIG['database'],
            charset='utf8mb4'
        )
        return conn
    except Error as e:
        app.logger.error(f"数据库连接错误: {e}")
        return None


def create_reader(openid, reader_card, reader_type):
    """创建新的读者记录"""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO readers (openid, reader_card, reader_type)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
            reader_card = VALUES(reader_card),
            reader_type = VALUES(reader_type)
        """, (openid, reader_card, reader_type))
        conn.commit()
        return True
    except Error as e:
        app.logger.error(f"创建读者记录错误: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


def get_reader(openid):
    """根据OpenID获取读者信息"""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT reader_card, reader_type
            FROM readers
            WHERE openid = %s
        """, (openid,))
        return cursor.fetchone()
    except Error as e:
        app.logger.error(f"获取读者信息错误: {e}")
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


def delete_reader(openid):
    """删除读者记录"""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM readers WHERE openid = %s", (openid,))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        app.logger.error(f"删除读者记录错误: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


# ====================== 微信接口处理 ======================
# 初始化推荐器
recommender = BookRecommender(DB_CONFIG)

# 用户会话状态管理
user_sessions = {}

# 定义支持的读者类型
SUPPORTED_TYPES = ['0', '1']  # 0=证件号, 1=条码号


def check_signature(signature, timestamp, nonce):
    """验证微信签名"""
    # 1. 将 token, timestamp, nonce 按字典序排序
    arr = sorted([WECHAT_TOKEN, timestamp, nonce])

    # 2. 将三个参数字符串拼接成一个字符串
    combined_str = ''.join(arr)

    # 3. 进行 SHA1 加密
    sha1 = hashlib.sha1()
    sha1.update(combined_str.encode('utf-8'))
    sha1_str = sha1.hexdigest()

    # 4. 将加密后的字符串与 signature 对比
    return sha1_str == signature


@app.route('/wechat', methods=['GET', 'POST'])
def wechat_handler():
    if request.method == 'GET':
        # 微信服务器验证
        signature = request.args.get('signature', '')
        timestamp = request.args.get('timestamp', '')
        nonce = request.args.get('nonce', '')
        echostr = request.args.get('echostr', '')

        # 验证签名
        if check_signature(signature, timestamp, nonce):
            return echostr  # 验证成功返回 echostr
        return ""  # 验证失败返回空字符串

    # 处理POST请求（用户消息）
    # 先验证签名
    signature = request.args.get('signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')

    if not check_signature(signature, timestamp, nonce):
        return "Invalid signature", 403

    # 解析XML消息
    try:
        xml_data = request.data
        root = ET.fromstring(xml_data)
        msg = {}
        for child in root:
            msg[child.tag] = child.text
    except Exception as e:
        app.logger.error(f"XML解析错误: {str(e)}")
        return "XML parse error", 400

    # 处理消息并回复
    reply_content = handle_message(msg)
    return generate_reply_xml(msg, reply_content)


def generate_reply_xml(msg, content):
    """生成回复XML"""
    xml = f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <xml>
    <ToUserName><![CDATA[{msg['FromUserName']}]]></ToUserName>
    <FromUserName><![CDATA[{msg['ToUserName']}]]></FromUserName>
    <CreateTime>{int(time.time())}</CreateTime>
    <MsgType><![CDATA[text]]></MsgType>
    <Content><![CDATA[{content}]]></Content>
    </xml>
    """
    response = make_response(xml)
    response.content_type = 'application/xml; charset=utf-8'
    return response


def handle_message(msg):
    """
    处理用户消息的主函数，分发到不同的处理器。
    """
    msg_type = msg.get('MsgType', '').lower()
    openid = msg.get('FromUserName')

    # ========== 1. 处理事件消息 ==========
    if msg_type == 'event':
        event = msg.get('Event', '').lower()

        # --- 处理关注/取消关注事件 ---
        if event == 'subscribe':
            welcome = get_welcome_message()
            help_msg = get_help_message()
            return f"{welcome}\n\n📌 您可以这样操作：\n{help_msg}"

        elif event == 'unsubscribe':
            app.logger.info(f"用户取消关注: {openid}")
            return ""  # 取消关注无需回复

        # --- 处理菜单点击事件 ---
        elif event == 'click':
            event_key = msg.get('EventKey', '')
            app.logger.info(f"用户 {openid} 点击了菜单，EventKey: {event_key}")

            if event_key == 'RECOMMEND_BOOKS':
                return process_recommendation(openid)

            elif event_key == 'BIND_ACCOUNT':
                return process_bind_request(openid)

            elif event_key == 'UNBIND_ACCOUNT':
                return process_unbind_request(openid)

            else:
                return "🔍 未知菜单项，功能正在快马加鞭地开发中..."

        # --- 处理其他未知事件 ---
        else:
            return "收到一个未知的事件类型，暂时无法处理哦。"

    # ========== 2. 处理文本消息 ==========
    elif msg_type == 'text':
        content = msg.get('Content', '').strip().lower()

        # 检查并更新会话
        if openid not in user_sessions:
            user_sessions[openid] = {'state': 'idle', 'last_active': time.time()}
        session_data = user_sessions[openid]
        session_data['last_active'] = time.time()
        clean_expired_sessions()  # 清理过期会话

        # --- 检查是否处于等待绑定信息的状态 ---
        if session_data.get('state') == 'awaiting_info':
            return process_binding(openid, session_data, msg.get('Content', '').strip())

        # --- 处理关键词指令 ---
        if content in ['推荐', 'tuijian']:
            return process_recommendation(openid)

        elif content in ['绑定', 'bangding', 'bd']:
            return process_bind_request(openid)

        elif content in ['解绑', 'jiebang', 'jb']:
            return process_unbind_request(openid)

        elif content in ['帮助', 'help', '?']:
            return get_help_message()

        else:
            # 默认回复，可以引导用户
            return get_welcome_message()

    # ========== 3. 处理其他消息类型 ==========
    else:
        return "🤖 暂时只支持文本和菜单点击哦，试试发送【帮助】吧！"


def clean_expired_sessions():
    """清理30分钟无活动的会话"""
    current_time = time.time()
    expired_users = []

    for user_id, session in user_sessions.items():
        if current_time - session['last_active'] > 1800:  # 30分钟
            expired_users.append(user_id)

    for user_id in expired_users:
        del user_sessions[user_id]


def process_binding(openid, session, input_str):
    """处理绑定信息"""
    # 解析输入
    parts = re.split(r'[，,]', input_str)
    parts = [p.strip() for p in parts]
    if len(parts) != 2:
        return "❌ 格式错误，请按 [读者证号],[读者类型] 格式输入\n例如: A123,0"

    reader_card, reader_type = parts

    # 验证读者类型
    if reader_type not in SUPPORTED_TYPES:
        return f"❌ 不支持的类型: {reader_type}\n请使用: 0(证件号) 或 1(条码号)"

    # 验证读者证号格式 (10位字母数字)
    if not re.match(r'^[a-zA-Z0-9]{10}$', reader_card):
        return "❌ 读者证号格式错误，请输入10位字符"

    # 保存到数据库
    if create_reader(openid, reader_card, reader_type):
        session['state'] = 'idle'
        reader_type_name = "证件号" if reader_type == '0' else "条码号"
        return f"✅ 绑定成功!\n类型: {reader_type_name}\n证号: {reader_card}\n\n发送【推荐】获取图书推荐"
    else:
        return "❌ 绑定失败，请稍后再试"


def process_unbind(openid):
    """处理解绑请求"""
    if delete_reader(openid):
        # 清除会话状态
        if openid in user_sessions:
            user_sessions[openid]['state'] = 'idle'
        return "✅ 已解除绑定\n\n您可以发送【绑定】重新绑定"
    return "⚠️ 解绑失败，或您尚未绑定"


def process_recommendation(openid):
    """处理推荐请求"""
    # 获取绑定信息
    reader_info = get_reader(openid)
    if not reader_info:
        return "⚠️ 您还未绑定，请先发送【绑定】完成注册"

    try:
        reader_id = reader_info['reader_card']
        reader_type = int(reader_info['reader_type'])  # 0 或 1

        # 调用 get_reading_history.py 中的函数获取历史记录
        history_items = get_reading_history(reader_id)

        if not history_items:
            fallback_books, _ = recommender.get_recommendations(reader_id, [], top_n=4)
            reply = "⚠️ 未找到您的借阅历史记录，请确认读者证号和类型是否正确。\n"
            reply += "📚 为您随机推荐以下图书：\n"
            reply += format_recommendations(fallback_books)
            return reply

        # 获取推荐书籍
        recommendations, error = recommender.get_recommendations(
            reader_id, history_items, top_n=4
        )

        if error:
            return f"❌ 获取推荐失败：{error}"

        reply = "📚 为您推荐以下精选书籍：\n"
        reply += format_recommendations(recommendations)
        return reply

    except Exception as e:
        app.logger.error(f"推荐处理异常: {str(e)}")
        return "⚠️ 系统繁忙，请稍后再试"


def format_recommendations(recommendations):
    """格式化推荐结果"""
    if not recommendations:
        return "📭 暂时没有找到合适的推荐，请稍后再试"

    reply = "\n"
    for i, book in enumerate(recommendations, 1):
        title = book.get("题名", "未知书名")
        author = book.get("责任者", "未知作者")
        callno = book.get("索书号", "")
        publisher = book.get("出版社", "未知出版社")
        year = book.get("出版年", "")
        intro = book.get("简介", "")

        reply += f"{i}. 《{title}》\n"
        reply += f" 👤 作者: {author}\n"
        reply += f" 🏷️ 索书号: {callno}\n"
        reply += f" 🏢 出版社: {publisher}\n"
        if year:
            reply += f" 📅 出版年: {year}\n"
        if intro:
            # 截取前100个字符，避免消息过长
            short_intro = (intro[:100] + '...') if len(intro) > 100 else intro
            reply += f" 📖 简介: {short_intro}\n"
        reply += "\n\n"

    reply += "🔍 在图书馆检索索书号即可找到对应书籍\n"
    reply += "🔄 再次发送【推荐】获取更多好书"
    return reply


def get_help_message():
    """帮助信息"""
    return """ 使用帮助：
1. 【绑定】 - 绑定读者信息 (格式: [证号],[类型])
2. 【推荐】 - 获取个性化图书推荐
3. 【解绑】 - 解除当前绑定
4. 类型说明:
   - 0 = 证件号
   - 1 = 条码号

 读者证号为字母数字组合
 系统根据您的借阅历史推荐书籍
 也可以在底部菜单中的读者服务中使用

再次感谢你的关注，愿这里的每一本书、每一段感悟，都能启迪你的智慧，为你的生活增添一抹书香与温暖。让我们以书为媒，相伴同行，在阅读的旅程中不断成长。"""


def get_welcome_message():
    """欢迎消息"""
    return """亲爱的书友，欢迎来到阅美西农书香世界！很高兴与你在文字的海洋中相遇，从此，我们将一同探索经典的奥秘，分享阅读的感动。​
在这里，我们会定期为你精选历经时间沉淀的经典图书，从文学巨著到社科佳作，从历史典籍到哲思小品，让每一次推荐都成为你与好书相遇的契机。同时，这里也是你的读书感言分享地，无论是掩卷沉思的顿悟，还是字里行间的共鸣，都可以在这里尽情抒发，与同频的书友碰撞思想的火花。

发送以下指令或在菜单中的读者服务选择：
【绑定】 - 绑定读者信息
【推荐】 - 获取图书推荐
【帮助】 - 查看使用说明"""


# ====================== 定时推荐处理 ======================
def scheduled_recommendation():
    """每半个月执行一次的定时推荐任务"""
    app.logger.info(f"开始执行定时推荐任务: {datetime.now()}")

    # 获取所有已绑定的读者
    conn = get_db_connection()
    if not conn:
        app.logger.error("定时推荐任务：数据库连接失败")
        return

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT openid, reader_card, reader_type FROM readers")
        readers = cursor.fetchall()
        app.logger.info(f"定时推荐任务：共找到{len(readers)}位读者")

        for reader in readers:
            try:
                openid = reader['openid']
                reader_id = reader['reader_card']
                reader_type = int(reader['reader_type'])

                # 获取阅读历史
                history_items = get_reading_history(reader_id, max_pages=1, page_size=20)

                # 获取推荐书籍
                reply = '📚 为您定时推荐以下图书：\n'
                recommendations, _ = recommender.get_recommendations(
                    reader_id, history_items if history_items else [], top_n=4
                )
                reply += format_recommendations(recommendations)
                if recommendations:
                    # 发送微信通知
                    send_wechat_notification(openid, reply)
                    app.logger.info(f"已为读者 {reader_id} 发送推荐通知")
                else:
                    app.logger.warning(f"读者 {reader_id} 无推荐结果")

            except Exception as e:
                app.logger.error(f"处理读者 {reader.get('reader_card', '')} 时出错: {str(e)}")

    except Error as e:
        app.logger.error(f"定时推荐任务：数据库错误: {str(e)}")
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


def send_wechat_notification(openid, content):
    """发送微信客服消息"""
    access_token = get_wechat_access_token()
    if not access_token:
        return

    url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={access_token}"
    payload = {
        "touser": openid,
        "msgtype": "text",
        "text": {
            "content": content
        }
    }
    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, data=json.dumps(payload, ensure_ascii=False), headers=headers)
        result = response.json()
        if result.get('errcode') != 0:
            app.logger.error(f"发送微信通知失败: {result}")
    except Exception as e:
        app.logger.error(f"发送微信通知异常: {str(e)}")


def get_wechat_access_token():
    """获取微信访问令牌"""
    # 从环境变量获取微信凭证
    appid = WECHAT_APPID
    secret = WECHAT_SECRET

    if not appid or not secret:
        app.logger.error("未配置微信公众号凭证")
        return None

    url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
    try:
        response = requests.get(url)
        data = response.json()
        return data.get('access_token')
    except Exception as e:
        app.logger.error(f"获取微信访问令牌失败: {str(e)}")
        return None


def schedule_next_recommendation():
    """安排下一次推荐任务"""
    # 计算下一个推荐日期（每月1号和16号）
    now = datetime.now()
    if now.day < 16:
        next_date = now.replace(day=1) + timedelta(days=15)
    else:
        # 下个月1号
        next_date = (now.replace(day=28) + timedelta(days=4)).replace(day=1)

    # 设置执行时间为上午10点
    next_date = next_date.replace(hour=10, minute=0, second=0, microsecond=0)

    app.logger.info(f"安排下一次定时推荐: {next_date}")

    # 添加定时任务
    scheduler.add_job(
        execute_scheduled_recommendation,
        'date',
        run_date=next_date
    )


def execute_scheduled_recommendation():
    """执行定时推荐并安排下一次任务"""
    try:
        scheduled_recommendation()
    finally:
        # 无论成功与否，都安排下一次任务
        schedule_next_recommendation()


scheduler = BackgroundScheduler()
scheduler.start()


# ====================== 网页前端路由 ======================
@app.route('/')
def web_index():
    # print("\n--- 2. 进入 / (首页) ---")
    # 1. 尝试从 session 获取 openid，并查询读者信息
    openid = session.get('openid')
    # print(f"在首页获取到的 session['openid']: {openid}")
    # print(f"首页的 session 内容: {dict(session)}")
    current_reader = None  # 初始化当前读者信息为空
    if openid:
        # 如果用户已通过微信授权登录，就去数据库查询他的信息
        conn_temp = get_db_connection()
        if conn_temp:
            cursor_temp = conn_temp.cursor(dictionary=True)
            cursor_temp.execute("SELECT * FROM readers WHERE openid = %s", (openid,))
            current_reader = cursor_temp.fetchone()
            cursor_temp.close()
            conn_temp.close()

    # 2. 获取分页、搜索等参数（这部分逻辑保持不变）
    page = int(request.args.get('page', 1))
    per_page = 8  # 建议将每页数量定义在函数开头，方便修改
    offset = (page - 1) * per_page
    query = request.args.get('q', '')

    # 3. 查询书籍列表和总数（这部分逻辑保持不变）
    conn = get_db_connection()
    if not conn:
        return "数据库连接失败", 500

    cursor = conn.cursor(dictionary=True)

    # 获取总书籍数量
    if query:
        cursor.execute("SELECT COUNT(*) AS total FROM books WHERE 题名 LIKE %s", (f"%{query}%",))
    else:
        cursor.execute("SELECT COUNT(*) AS total FROM books")
    total_books = cursor.fetchone()['total']
    total_pages = (total_books + per_page - 1) // per_page

    # 获取当前页书籍
    if query:
        cursor.execute("SELECT * FROM books WHERE 题名 LIKE %s LIMIT %s OFFSET %s", (f"%{query}%", per_page, offset))
    else:
        cursor.execute("SELECT * FROM books LIMIT %s OFFSET %s", (per_page, offset))
    books = cursor.fetchall()

    cursor.close()
    conn.close()

    # 计算分页区间
    start_page = max(1, page - 3)
    end_page = min(total_pages, page + 3)
    # print("准备渲染 index.html...")
    # 4. 将所有需要的数据，包括当前读者信息，一起传递给模板
    return render_template(
        'index.html',
        books=books,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page,
        query=query,
        current_reader=current_reader  # 将当前读者信息传递给前端
    )

@app.route('/book/<int:book_id>')
def book_detail(book_id):
    # 1. 尝试从 session 获取 openid，并查询读者信息
    openid = session.get('openid')
    current_reader = None  # 初始化当前读者信息为空
    if openid:
        # 如果用户已通过微信授权登录，就去数据库查询他的信息
        conn_temp = get_db_connection()
        if conn_temp:
            cursor_temp = conn_temp.cursor(dictionary=True)
            cursor_temp.execute("SELECT * FROM readers WHERE openid = %s", (openid,))
            current_reader = cursor_temp.fetchone()
            cursor_temp.close()
            conn_temp.close()

    # 2. 查询书籍、推荐和感悟的核心逻辑（基本保持不变）
    conn = get_db_connection()
    if not conn:
        return "数据库连接失败", 500

    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM books WHERE 序号 = %s", (book_id,))
    book = cursor.fetchone()
    if not book:
        return "书籍不存在", 404

    cursor.execute("SELECT * FROM books WHERE 责任者 = %s AND 序号 != %s LIMIT 5", (book['责任者'], book_id))
    recommendations = cursor.fetchall()

    cursor.execute("""
        SELECT 
            r.id as reflection_id,
            r.content, r.timestamp,
            readers.reader_card, 
            readers.nickname, 
            CASE readers.reader_type WHEN '0' THEN '证件号' ELSE '条码号' END as reader_type_text,
            (SELECT COUNT(*) FROM likes l WHERE l.reflection_id = r.id) AS likes
        FROM reflections r
        JOIN readers ON r.reader_id = readers.id
        WHERE r.book_id = %s AND r.status = 1
        ORDER BY r.timestamp DESC
    """, (book_id,))
    reflections = cursor.fetchall()  # 'reflections' 是一个字典列表，每个字典就是一个 'r'

    cursor.close()
    conn.close()

    # 3. 将所有需要的数据，包括当前读者信息，一起传递给模板
    return render_template(
        'book_detail.html',
        book=book,
        reflections=reflections,
        recommendations=recommendations,
        current_reader=current_reader  # 将当前读者信息传递给前端
    )

@app.route('/post_reflection', methods=['POST'])
def post_reflection():
    book_id = request.form.get('book_id')
    reader_card = request.form.get('reader_card')
    content = request.form.get('content')

    if not all([book_id, reader_card, content]):
        return "缺少参数", 400

    # 查 reader_id（内部主键）
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM readers WHERE reader_card = %s", (reader_card,))
    reader = cursor.fetchone()

    if not reader:
        return "读者证号无效", 400

    reader_id = reader['id']

    # 插入感悟
    cursor.execute(
        "INSERT INTO reflections (content, book_id, reader_id) VALUES (%s, %s, %s)",
        (content, book_id, reader_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('book_detail', book_id=book_id))


@app.route('/like', methods=['POST'])
def like_reflection():
    reflection_id = request.form.get('reflection_id')
    reader_card = request.form.get('reader_card')

    if not reflection_id or not reader_card:
        return jsonify({'error': '缺少参数'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 查找 reader_id
    cursor.execute("SELECT id FROM readers WHERE reader_card = %s", (reader_card,))
    reader = cursor.fetchone()
    if not reader:
        return jsonify({'error': '读者不存在'}), 400
    reader_id = reader['id']

    # 检查是否点过赞
    cursor.execute(
        "SELECT * FROM likes WHERE reflection_id = %s AND reader_id = %s",
        (reflection_id, reader_id)
    )
    liked = cursor.fetchone()

    if not liked:
        cursor.execute(
            "INSERT INTO likes (reflection_id, reader_id) VALUES (%s, %s)",
            (reflection_id, reader_id)
        )
        conn.commit()

    # 获取最新点赞数
    cursor.execute(
        "SELECT COUNT(*) AS likes FROM likes WHERE reflection_id = %s",
        (reflection_id,)
    )
    likes = cursor.fetchone()['likes']

    cursor.close()
    conn.close()
    return jsonify({'likes': likes})


@app.route('/my_page')
def my_page():
    # 1. 从 session 中获取 openid
    openid = session.get('openid')
    if not openid:
        # 如果 session 中没有 openid，说明用户没有通过微信授权访问
        # 可以提示用户从微信菜单进入，或者直接重定向到授权链接
        # 这里我们返回一个提示页面
        return "请从微信公众号菜单访问此页面以完成授权。", 403

    # 2. 连接数据库
    conn = get_db_connection()
    if not conn:
        return "数据库连接失败，请稍后再试。", 500

    cursor = conn.cursor(dictionary=True)

    try:
        # 3. 使用 openid 查询 readers 表，获取读者信息
        # 这是核心逻辑的改变：从 openid 找到 reader
        cursor.execute("SELECT * FROM readers WHERE openid = %s", (openid,))
        reader = cursor.fetchone()

        if not reader:
            # 如果数据库中没有这个 openid 对应的读者，说明用户还未绑定
            cursor.close()
            conn.close()
            # 可以渲染一个提示绑定的页面，或者直接返回文本
            return "您尚未绑定读者证，请在公众号对话框中发送【绑定】进行操作。", 404

        # 4. 如果找到了读者，使用读者的 ID (reader['id']) 去查询相关的感悟
        cursor.execute("""
                SELECT 
                    r.id AS reflection_id,
                    r.content, 
                    r.timestamp,
                    COALESCE(b.题名, ub.title) AS book_title,
                    (SELECT COUNT(*) FROM likes l WHERE l.reflection_id = r.id) AS likes
                FROM reflections r
                LEFT JOIN books b ON r.book_id = b.序号
                LEFT JOIN user_books ub ON r.user_book_id = ub.id
                WHERE r.reader_id = %s AND r.status = 1
                ORDER BY r.timestamp DESC
            """, (reader['id'],))
        reflections = cursor.fetchall()

    except Error as e:
        app.logger.error(f"查询我的主页信息时出错: {e}")
        return "查询信息时发生错误，请稍后重试。", 500
    finally:
        # 确保数据库连接被关闭
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

    # 5. 将查询到的读者信息和感悟列表传递给模板
    return render_template("my_page.html", reader=reader, reflections=reflections)


@app.route('/wechat_redirect')
def wechat_redirect():
    # print("\n--- 1. 进入 /wechat_redirect ---")
    # 从微信回调中获取 code
    code = request.args.get('code')
    # print(f"收到的 code: {code}")
    if not code:
        # print("错误：没有收到 code 参数。")
        return "授权失败，请重试", 400

    # 使用 code 获取 access_token 和 openid
    appid = WECHAT_APPID
    secret = WECHAT_SECRET
    token_url = f"https://api.weixin.qq.com/sns/oauth2/access_token?appid={appid}&secret={secret}&code={code}&grant_type=authorization_code"

    try:
        # print("正在请求 access_token 和 openid...")
        response = requests.get(token_url)
        data = response.json()
        # print(f"微信返回的数据: {data}")  # 打印微信的完整响应

        openid = data.get('openid')
        if not openid:
            # print(f"错误：获取 openid 失败。微信返回: {data}")
            return "获取用户信息失败", 400

        # 将 openid 存入 session
        session['openid'] = openid
        # print(f"成功获取并设置 session['openid']: {session['openid']}")
        # # 检查 session 是否真的存进去了
        # print(f"当前 session 内容: {dict(session)}")

        # 重定向到主页面
        # print("--- 准备重定向到首页 ---")
        return redirect(url_for('web_index'))

    except Exception as e:
        app.logger.error(f"微信授权失败: {str(e)}")
        # print(f"微信授权流程异常: {str(e)}")  # 确保在终端能看到
        return "授权失败，请稍后再试", 500


def create_wechat_menu():
    """创建微信公众号自定义菜单"""
    access_token = get_wechat_access_token()
    if not access_token:
        return False

    # 菜单配置
    menu_data = {
        "button": [
            {
                "type": "view",
                "name": "图书首页",
                "url": f"https://open.weixin.qq.com/connect/oauth2/authorize?appid={WECHAT_APPID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=snsapi_base&state=STATE#wechat_redirect"
            },
            {
                "name": "读者服务",
                "sub_button": [
                    {
                        "type": "click",
                        "name": "账号绑定",
                        "key": "BIND_ACCOUNT"
                    },
                    {
                        "type": "click",
                        "name": "图书推荐",
                        "key": "RECOMMEND_BOOKS"
                    }
                ]
            }
        ]
    }

    url = f"https://api.weixin.qq.com/cgi-bin/menu/create?access_token={access_token}"
    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, data=json.dumps(menu_data, ensure_ascii=False), headers=headers)
        result = response.json()
        if result.get('errcode') == 0:
            print("创建成功")
            app.logger.info("微信公众号菜单创建成功")
            return True
        else:
            print("创建失败")
            app.logger.error(f"菜单创建失败: {result}")
            return False
    except Exception as e:
        app.logger.error(f"创建菜单异常: {str(e)}")
        return False


@app.route('/reflections', methods=['GET', 'POST'])
def reflections_square():
    # 1. 身份验证
    openid = session.get('openid')
    if not openid:
        return "请从微信公众号菜单访问此页面以完成授权。", 403

    conn = get_db_connection()
    if not conn:
        return "数据库连接失败", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM readers WHERE openid = %s", (openid,))
    current_reader = cursor.fetchone()
    if not current_reader:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()
        return "您尚未绑定读者证，请在公众号对话框中发送【绑定】进行操作。", 404

    # 2. 处理 POST 请求 (发布新感悟)
    if request.method == 'POST':
        book_title = request.form.get('book_title', '').strip()
        content = request.form.get('content', '').strip()

        if not book_title or not content:
            return "书名和感悟内容都不能为空！", 400

        book_id = None
        user_book_id = None

        try:
            # 步骤 2.1: 首先在官方 `books` 表中查找
            cursor.execute("SELECT 序号 FROM books WHERE 题名 = %s LIMIT 1", (book_title,))
            book = cursor.fetchone()

            if book:
                # 在官方库中找到了
                book_id = book['序号']
            else:
                # 步骤 2.2: 在 `user_books` 表中查找是否已存在
                cursor.execute("SELECT id FROM user_books WHERE title = %s LIMIT 1", (book_title,))
                user_book = cursor.fetchone()
                if user_book:
                    # 在用户创建的书中找到了
                    user_book_id = user_book['id']
                else:
                    # 步骤 2.3: 两边都没找到，创建新的 `user_books` 记录
                    cursor.execute(
                        "INSERT INTO user_books (title, reader_id) VALUES (%s, %s)",
                        (book_title, current_reader['id'])
                    )
                    # 获取刚刚插入的新书的ID
                    user_book_id = cursor.lastrowid

            # 步骤 2.4: 插入感悟记录
            cursor.execute(
                "INSERT INTO reflections (book_id, user_book_id, reader_id, content, timestamp) VALUES (%s, %s, %s, %s, NOW())",
                (book_id, user_book_id, current_reader['id'], content)
            )
            conn.commit()
            return redirect(url_for('reflections_square'))

        except Error as e:
            app.logger.error(f"处理新感悟时出错: {e}")
            conn.rollback()
            return "发布失败，数据库发生错误，请稍后重试。", 500
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()

    # 3. 处理 GET 请求 (显示感悟列表)
    try:
        page = int(request.args.get('page', 1))
        per_page = 10
        offset = (page - 1) * per_page

        cursor.execute("SELECT COUNT(*) AS total FROM reflections WHERE status = 1")
        total_reflections = cursor.fetchone()['total']
        total_pages = (total_reflections + per_page - 1) // per_page

        # 使用 COALESCE 和 LEFT JOIN 来合并查询
        cursor.execute("""
            SELECT 
                r.id AS reflection_id,
                r.content, 
                r.timestamp,
                COALESCE(b.题名, ub.title) AS book_title,
                readers.reader_card,
                readers.nickname, 
                (SELECT COUNT(*) FROM likes l WHERE l.reflection_id = r.id) AS likes_count
            FROM reflections r
            LEFT JOIN books b ON r.book_id = b.序号
            LEFT JOIN user_books ub ON r.user_book_id = ub.id
            JOIN readers ON r.reader_id = readers.id
            WHERE r.status = 1
            ORDER BY r.timestamp DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        reflections = cursor.fetchall()

        start_page = max(1, page - 3)
        end_page = min(total_pages, page + 3)

        return render_template(
            'reflections.html',
            reflections=reflections,
            page=page,
            total_pages=total_pages,
            start_page=start_page,
            end_page=end_page,
            current_reader=current_reader
        )
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


@app.route('/reflection/delete/<int:reflection_id>', methods=['POST'])
def delete_reflection(reflection_id):
    # 步骤 1: 验证用户是否登录且为管理员
    openid = session.get('openid')
    if not openid:
        return "请先登录", 403

    conn = get_db_connection()
    if not conn:
        return "数据库连接失败", 500

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT is_admin FROM readers WHERE openid = %s", (openid,))
        reader = cursor.fetchone()

        if not reader or not reader.get('is_admin'):
            return "无权限操作", 403  # 403 Forbidden

        # 步骤 2: 执行软删除 (更新 status 字段)
        cursor.execute("UPDATE reflections SET status = 0 WHERE id = %s", (reflection_id,))
        conn.commit()

        # 步骤 3: 操作成功后，重定向回用户刚才所在的页面
        # request.referrer 能获取到上一个页面的 URL，非常方便
        return redirect(request.referrer or url_for('web_index'))

    except Error as e:
        conn.rollback()
        app.logger.error(f"删除感悟(id={reflection_id})失败: {e}")
        return "删除失败，服务器发生错误", 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


@app.route('/my_reflection/delete/<int:reflection_id>', methods=['POST'])
def delete_my_reflection(reflection_id):
    openid = session.get('openid')
    if not openid:
        return "请先登录", 403

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. 验证这条感悟是否属于当前登录用户
        cursor.execute("""
            SELECT r.id FROM reflections r
            JOIN readers re ON r.reader_id = re.id
            WHERE r.id = %s AND re.openid = %s
        """, (reflection_id, openid))
        reflection = cursor.fetchone()

        if not reflection:
            return "无权限操作或该感悟不存在", 403

        # 2. 执行软删除 (更新 status = 0)
        cursor.execute("UPDATE reflections SET status = 0 WHERE id = %s", (reflection_id,))
        conn.commit()

        # 3. 重定向回个人主页
        return redirect(url_for('my_page'))

    except Error as e:
        conn.rollback()
        app.logger.error(f"用户 {openid} 删除感悟(id={reflection_id})失败: {e}")
        return "删除失败，服务器发生错误", 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


@app.route('/my_reflection/edit/<int:reflection_id>', methods=['GET', 'POST'])
def edit_reflection(reflection_id):
    openid = session.get('openid')
    if not openid:
        return "请先登录", 403

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. 先验证这条感悟是否属于当前登录用户
        cursor.execute("""
            SELECT r.id, r.content, COALESCE(b.题名, ub.title) as book_title
            FROM reflections r
            LEFT JOIN books b ON r.book_id = b.序号
            LEFT JOIN user_books ub ON r.user_book_id = ub.id
            JOIN readers re ON r.reader_id = re.id
            WHERE r.id = %s AND re.openid = %s AND r.status = 1
        """, (reflection_id, openid))
        reflection = cursor.fetchone()

        if not reflection:
            return "无权限操作或该感悟不存在", 403

        # 2. 如果是 POST 请求，处理表单提交
        if request.method == 'POST':
            new_content = request.form.get('content', '').strip()
            if not new_content:
                # 可以返回错误信息给模板，这里简化处理
                return "感悟内容不能为空！", 400

            cursor.execute("UPDATE reflections SET content = %s WHERE id = %s", (new_content, reflection_id))
            conn.commit()

            # 编辑成功后，重定向回个人主页
            return redirect(url_for('my_page'))

        # 3. 如果是 GET 请求，显示编辑页面
        return render_template('edit_reflection.html', reflection=reflection)

    except Error as e:
        conn.rollback()
        app.logger.error(f"用户 {openid} 编辑感悟(id={reflection_id})失败: {e}")
        return "操作失败，服务器发生错误", 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


@app.route('/profile/edit', methods=['GET', 'POST'])
def edit_profile():
    # 1. 身份验证
    openid = session.get('openid')
    if not openid:
        return "请先登录", 403

    conn = get_db_connection()
    if not conn:
        return "数据库连接失败", 500
    cursor = conn.cursor(dictionary=True)

    try:
        # 2. 获取当前用户信息
        cursor.execute("SELECT * FROM readers WHERE openid = %s", (openid,))
        current_reader = cursor.fetchone()
        if not current_reader:
            return "读者信息不存在", 404

        # 3. 如果是 POST 请求，处理表单提交
        if request.method == 'POST':
            new_nickname = request.form.get('nickname', '').strip()

            # 简单的验证
            if not new_nickname:
                # 使用 flash 消息是更好的方式，这里先用简单返回
                return "昵称不能为空！", 400
            if len(new_nickname) > 50:
                return "昵称不能超过50个字符！", 400

            # 更新数据库
            cursor.execute("UPDATE readers SET nickname = %s WHERE id = %s",
                           (new_nickname, current_reader['id']))
            conn.commit()

            # 修改成功后，重定向到个人主页
            return redirect(url_for('my_page'))

        # 4. 如果是 GET 请求，显示编辑页面
        return render_template('edit_profile.html', reader=current_reader)

    except Error as e:
        conn.rollback()
        app.logger.error(f"编辑个人资料失败: {e}")
        return "操作失败，服务器错误", 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


def process_bind_request(openid):
    """
    处理绑定请求的通用函数。
    无论是用户发送文本'绑定'还是点击菜单，都调用此函数。
    """
    reader_info = get_reader(openid)
    if reader_info:
        reader_type_name = "证件号" if reader_info.get('reader_type') == '0' else "条码号"
        return f"⚠️ 您已绑定: {reader_type_name}, 证号 {reader_info.get('reader_card')}\n\n如需重新绑定，请先发送或点击【解绑】。"

    # 初始化或更新用户会话状态
    if openid not in user_sessions:
        user_sessions[openid] = {'state': 'idle', 'last_active': time.time()}
    session_data = user_sessions[openid]
    session_data['state'] = 'awaiting_info'
    session_data['last_active'] = time.time()

    return "📝 请按格式输入: [读者证号],[读者类型]\n\n例如: A123,0\n\n类型说明:\n0=证件号(默认)\n1=条码号"


def process_unbind_request(openid):
    """
    处理解绑请求的通用函数。
    """
    if delete_reader(openid):
        # 清除可能存在的会话状态
        if openid in user_sessions:
            user_sessions[openid]['state'] = 'idle'
        return "✅ 已解除绑定。\n\n您可以再次【绑定】新的读者信息。"
    return "⚠️ 解绑失败，或您尚未绑定。"



# ====================== 主程序入口 ======================
if __name__ == '__main__':
    # 首次启动时安排任务
    schedule_next_recommendation()

    # 创建微信公众号菜单
    if os.getenv('CREATE_MENU', 'False') == 'True':
        if create_wechat_menu():
            app.logger.info("微信公众号菜单创建成功")
        else:
            app.logger.warning("微信公众号菜单创建失败")

    # 获取环境变量中的端口
    port = int(os.getenv('PORT', 80))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('DEBUG', 'False') == 'True')