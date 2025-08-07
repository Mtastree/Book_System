import requests
import json
import time
import uuid
import hashlib
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


def generate_hw_apiheader(app_id, app_key):
    """生成汇文API认证头"""
    noncestr = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    string_a = f"appId={app_id}&noncestr={noncestr}&timestamp={timestamp}"
    str_sign_temp = string_a + "&key=" + app_key
    sign = hashlib.md5(str_sign_temp.encode('utf-8')).hexdigest()
    return json.dumps({
        "appId": app_id,
        "noncestr": noncestr,
        "timestamp": timestamp,
        "sign": sign
    }, separators=(',', ':'))


def get_reading_history(reader_id, id_type=0, max_pages=1, page_size=10):
    """
    获取读者借阅历史记录

    参数:
    reader_id: 读者证件号或条码号
    id_type: 读者ID类型 (0=证件号, 1=条码号)，默认0
    max_pages: 最大获取页数，默认1
    page_size: 每页记录数，默认10

    返回:
    借阅历史记录列表，格式: [{"callNo": "索书号1"}, {"callNo": "索书号2"}, ...]
    """
    # 从环境变量获取配置
    BASE_URL = os.getenv('HW_BASE_URL', "https://libopac.nwafu.edu.cn/meta-local/api")
    APP_ID = os.getenv('HW_APP_ID', "")
    APP_KEY = os.getenv('HW_APP_KEY', "")

    all_loans = []
    current_page = 1

    while current_page <= max_pages:
        # 生成认证头
        auth_header = generate_hw_apiheader(APP_ID, APP_KEY)

        # 构造请求
        headers = {
            "X-Hw-ApiAuth": auth_header,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "id": reader_id,
            "type": id_type,
            "currentPage": current_page,
            "pageSize": page_size
        }

        try:
            # 发送请求
            response = requests.post(
                url=f"{BASE_URL}/v1/patron/loan_histories",
                headers=headers,
                data=data,
                timeout=10  # 10秒超时
            )

            # 检查响应状态
            if response.status_code != 200:
                print(f"API请求失败，状态码: {response.status_code}")
                break

            # 解析JSON响应
            response_data = response.json()

            # 检查API返回码
            if response_data.get('code') != 0:
                print(f"API返回错误: {response_data.get('message', '未知错误')}")
                break

            # 提取借阅记录
            loans = response_data.get('data', {}).get('items', [])
            if not loans:
                break

            # 转换为所需格式
            for loan in loans:
                # 确保callNo字段存在
                call_no = loan.get('callNo', '')
                if call_no:
                    all_loans.append({"callNo": call_no, "readerId": reader_id})

            # 检查是否还有更多页面
            total = response_data.get('data', {}).get('total', 0)
            if current_page * page_size >= total:
                break

            current_page += 1

        except requests.exceptions.RequestException as e:
            print(f"网络请求异常: {str(e)}")
            break
        except json.JSONDecodeError:
            print("响应解析错误: 无效的JSON格式")
            break
    # print(all_loans)
    return all_loans


# if __name__ == '__main__':

#     get_reading_history("2014120020")
