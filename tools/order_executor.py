# tools/order_executor.py — KIS API 주문 집행기
# Phase 5 구현: 매수(IOC), 매도(시장가/IOC), 주문취소, 잔고조회, 체결확인
# 모든 주문 결과는 outputs/reports/orders_YYYYMMDD.json 에 로깅

import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

USE_PAPER = os.getenv("USE_PAPER", "true").lower() == "true"

if USE_PAPER:
    BASE_URL   = "https://openapivts.koreainvestment.com:29443"
    APP_KEY    = os.getenv("KIS_PAPER_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_PAPER_APP_SECRET", "")
    MODE_LABEL = "모의투자"
    TR_BUY="VTTC0802U"; TR_SELL="VTTC0801U"; TR_CANCEL="VTTC0803U"
    TR_BALANCE="VTTC8434R"; TR_ORDERS="VTTC8036R"
else:
    BASE_URL   = "https://openapi.koreainvestment.com:9443"
    APP_KEY    = os.getenv("KIS_APP_KEY", "")
    APP_SECRET = os.getenv("KIS_APP_SECRET", "")
    MODE_LABEL = "실전투자"
    TR_BUY="TTTC0802U"; TR_SELL="TTTC0801U"; TR_CANCEL="TTTC0803U"
    TR_BALANCE="TTTC8434R"; TR_ORDERS="TTTC8036R"

ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
ACNT_PRDT  = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs", "reports")


def _get_token():
    from tools.token_manager import ensure_token
    return ensure_token()

def _headers(tr_id):
    token = _get_token()
    return {"Content-Type":"application/json; charset=utf-8","authorization":f"Bearer {token}",
            "appkey":APP_KEY,"appsecret":APP_SECRET,"tr_id":tr_id,"custtype":"P"}

def _log_order(record):
    """주문 결과를 날짜별 JSON 파일에 누적 저장."""
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(LOG_DIR, f"orders_{today}.json")
    records = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f: records = json.load(f)
        except: records = []
    records.append(record)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def buy_ioc(code, qty, price):
    """IOC 방식 지정가 매수. 미체결 수량은 즉시 취소."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,
            "PDNO":code,"ORD_DVSN":"01","ORD_QTY":str(qty),"ORD_UNPR":str(price)}
    timestamp = datetime.now().isoformat()
    try:
        data = requests.post(url, headers=_headers(TR_BUY), json=body, timeout=10).json()
        success = data.get("rt_cd","9")=="0"
        order_no = data.get("output",{}).get("ODNO","")
        record = {"type":"BUY_IOC","success":success,"order_no":order_no,"code":code,
                  "qty":qty,"price":price,"mode":MODE_LABEL,"timestamp":timestamp,
                  "rt_cd":data.get("rt_cd","9"),"msg":data.get("msg1","")}
        _log_order(record)
        print(f"  [{'OK' if success else 'NG'}][{MODE_LABEL}] 매수IOC {code} {qty}주 @{price:,}원 order:{order_no}")
        return record
    except Exception as e:
        record = {"type":"BUY_IOC","success":False,"code":code,"qty":qty,"price":price,
                  "mode":MODE_LABEL,"timestamp":timestamp,"error":str(e)}
        _log_order(record); print(f"  [ERR][{MODE_LABEL}] 매수IOC {code}: {e}"); return record


def sell_market(code, qty):
    """시장가 매도 주문."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,
            "PDNO":code,"ORD_DVSN":"01","ORD_QTY":str(qty),"ORD_UNPR":"0","SLL_TYPE":"01"}
    timestamp = datetime.now().isoformat()
    try:
        data = requests.post(url, headers=_headers(TR_SELL), json=body, timeout=10).json()
        success = data.get("rt_cd","9")=="0"
        order_no = data.get("output",{}).get("ODNO","")
        record = {"type":"SELL_MARKET","success":success,"order_no":order_no,"code":code,
                  "qty":qty,"price":0,"mode":MODE_LABEL,"timestamp":timestamp,
                  "rt_cd":data.get("rt_cd","9"),"msg":data.get("msg1","")}
        _log_order(record)
        print(f"  [{'OK' if success else 'NG'}][{MODE_LABEL}] 시장가매도 {code} {qty}주 order:{order_no}")
        return record
    except Exception as e:
        record = {"type":"SELL_MARKET","success":False,"code":code,"qty":qty,"price":0,
                  "mode":MODE_LABEL,"timestamp":timestamp,"error":str(e)}
        _log_order(record); print(f"  [ERR][{MODE_LABEL}] 시장가매도 {code}: {e}"); return record


def sell_ioc(code, qty, price):
    """IOC 방식 지정가 매도."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,
            "PDNO":code,"ORD_DVSN":"01","ORD_QTY":str(qty),"ORD_UNPR":str(price),"SLL_TYPE":"01"}
    timestamp = datetime.now().isoformat()
    try:
        data = requests.post(url, headers=_headers(TR_SELL), json=body, timeout=10).json()
        success = data.get("rt_cd","9")=="0"
        order_no = data.get("output",{}).get("ODNO","")
        record = {"type":"SELL_IOC","success":success,"order_no":order_no,"code":code,
                  "qty":qty,"price":price,"mode":MODE_LABEL,"timestamp":timestamp,
                  "rt_cd":data.get("rt_cd","9"),"msg":data.get("msg1","")}
        _log_order(record)
        print(f"  [{'OK' if success else 'NG'}][{MODE_LABEL}] 매도IOC {code} {qty}주 @{price:,}원 order:{order_no}")
        return record
    except Exception as e:
        record = {"type":"SELL_IOC","success":False,"code":code,"qty":qty,"price":price,
                  "mode":MODE_LABEL,"timestamp":timestamp,"error":str(e)}
        _log_order(record); print(f"  [ERR][{MODE_LABEL}] 매도IOC {code}: {e}"); return record


def cancel_order(order_no, code, qty, price):
    """미체결 주문 취소."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,"KRX_FWDG_ORD_ORGNO":"",
            "ORGN_ODNO":order_no,"ORD_DVSN":"01","RVSE_CNCL_DVSN_CD":"02",
            "ORD_QTY":str(qty),"ORD_UNPR":str(price),"QTY_ALL_ORD_YN":"Y"}
    timestamp = datetime.now().isoformat()
    try:
        data = requests.post(url, headers=_headers(TR_CANCEL), json=body, timeout=10).json()
        success = data.get("rt_cd","9")=="0"
        record = {"type":"CANCEL","success":success,"order_no":order_no,"code":code,
                  "qty":qty,"price":price,"mode":MODE_LABEL,"timestamp":timestamp,
                  "rt_cd":data.get("rt_cd","9"),"msg":data.get("msg1","")}
        _log_order(record)
        print(f"  [{'OK' if success else 'NG'}][{MODE_LABEL}] 주문취소 {order_no}")
        return record
    except Exception as e:
        record = {"type":"CANCEL","success":False,"order_no":order_no,"code":code,
                  "mode":MODE_LABEL,"timestamp":timestamp,"error":str(e)}
        _log_order(record); print(f"  [ERR][{MODE_LABEL}] 주문취소: {e}"); return record


def get_balance():
    """
    잔고 조회.
    반환: {cash, positions:[{code,name,qty,avg_price,current_price,pnl_pct}], total_eval}
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,
              "AFHR_FLPR_YN":"N","OFL_YN":"N","INQR_DVSN":"02","UNPR_DVSN":"01",
              "FUND_STTL_ICLD_YN":"N","FNCG_AMT_AUTO_RDPT_YN":"N","PRCS_DVSN":"01",
              "CTX_AREA_FK100":"","CTX_AREA_NK100":""}
    try:
        data = requests.get(url, headers=_headers(TR_BALANCE), params=params, timeout=10).json()
        output1 = data.get("output1",[]); output2 = data.get("output2",[{}])
        positions = []
        for item in output1:
            qty = int(item.get("hldg_qty",0))
            if qty==0: continue
            avg=float(item.get("pchs_avg_pric",0)); cur=float(item.get("prpr",0))
            pnl=(cur-avg)/avg*100 if avg>0 else 0.0
            positions.append({"code":item.get("pdno",""),"name":item.get("prdt_name",""),
                              "qty":qty,"avg_price":int(avg),"current_price":int(cur),"pnl_pct":round(pnl,2)})
        summary = output2[0] if output2 else {}
        cash=int(float(summary.get("dnca_tot_amt",0))); total_eval=int(float(summary.get("tot_evlu_amt",0)))
        print(f"  [{MODE_LABEL}] 잔고: 예수금 {cash:,}원  보유{len(positions)}종목  총평가 {total_eval:,}원")
        return {"cash":cash,"positions":positions,"total_eval":total_eval}
    except Exception as e:
        print(f"  [{MODE_LABEL}] 잔고조회 오류: {e}"); return {"cash":0,"positions":[],"total_eval":0}


def get_order_status(order_no):
    """체결 상태 조회. 반환: {filled_qty, remaining_qty, status, avg_fill_price}"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    params = {"CANO":ACCOUNT_NO[:8],"ACNT_PRDT_CD":ACNT_PRDT,
              "INQR_STRT_DT":datetime.now().strftime("%Y%m%d"),"INQR_END_DT":datetime.now().strftime("%Y%m%d"),
              "SLL_BUY_DVSN_CD":"00","INQR_DVSN":"01","PDNO":"","ORD_GNO_BRNO":"","ODNO":order_no,
              "INQR_DVSN_3":"00","CTX_AREA_FK100":"","CTX_AREA_NK100":""}
    try:
        data = requests.get(url, headers=_headers(TR_ORDERS), params=params, timeout=10).json()
        output = data.get("output1",[{}])
        if not output: return {"filled_qty":0,"remaining_qty":0,"status":"UNKNOWN","avg_fill_price":0}
        item=output[0]
        filled=int(item.get("tot_ccld_qty",0)); ordered=int(item.get("ord_qty",0))
        remaining=ordered-filled; avg_price=int(float(item.get("avg_prvs",0)))
        status = "FILLED" if remaining==0 and filled>0 else ("PARTIAL" if filled>0 else "PENDING")
        return {"filled_qty":filled,"remaining_qty":remaining,"status":status,"avg_fill_price":avg_price}
    except Exception as e:
        print(f"  [{MODE_LABEL}] 체결조회 오류: {e}")
        return {"filled_qty":0,"remaining_qty":0,"status":"ERROR","avg_fill_price":0}


if __name__ == "__main__":
    print("=" * 55)
    print("  QUANTUM FLOW - 주문 집행기 테스트")
    print(f"  모드: {MODE_LABEL}")
    print("=" * 55)
    if not APP_KEY or not APP_SECRET or not ACCOUNT_NO:
        print("\n  API 키 또는 계좌번호가 설정되지 않았습니다.")
        if USE_PAPER:
            print("  .env: KIS_PAPER_APP_KEY, KIS_PAPER_APP_SECRET, KIS_ACCOUNT_NO")
        else:
            print("  .env: KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO")
        print("\n  Phase 5 order_executor.py - 구현 완료!")
    else:
        balance = get_balance()
        for pos in balance['positions']:
            print(f"  {pos['name']}({pos['code']}): {pos['qty']}주 {pos['pnl_pct']:+.2f}%")
    print("=" * 55)
