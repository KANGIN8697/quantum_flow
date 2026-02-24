"""
report.py — 백테스트 성과 집계 + HTML 리포트 생성
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import asdict

logger = logging.getLogger("backtest.report")


def generate_html_report(result, output_path: str = "backtest/results/report.html"):
    """백테스트 결과 → HTML 리포트"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 결과를 dict로 변환
    if hasattr(result, '__dataclass_fields__'):
        data = asdict(result)
    else:
        data = result

    valid_days = [d for d in data["day_results"] if d.get("stocks")]
    all_stocks = []
    for d in valid_days:
        for s in d["stocks"]:
            s["test_date"] = d["date"]
            all_stocks.append(s)

    # 통계 계산
    positive_days = len([d for d in valid_days if d["avg_return"] > 0])
    negative_days = len([d for d in valid_days if d["avg_return"] <= 0])
    max_return = max((d["avg_return"] for d in valid_days), default=0)
    min_return = min((d["avg_return"] for d in valid_days), default=0)

    positive_stocks = len([s for s in all_stocks if s["return_pct"] > 0])
    negative_stocks = len([s for s in all_stocks if s["return_pct"] <= 0])
    stock_hit_rate = (positive_stocks / len(all_stocks) * 100) if all_stocks else 0

    # 날짜별 누적 수익
    cumulative = []
    cum_sum = 0
    for d in sorted(valid_days, key=lambda x: x["date"]):
        cum_sum += d["avg_return"]
        cumulative.append({"date": d["date"], "cumulative": round(cum_sum, 2)})

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Quantum Flow 백테스트 리포트</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
    .container {{ max-width: 1200px; margin: 0 auto; }}
    h1 {{ color: #58a6ff; margin-bottom: 10px; font-size: 28px; }}
    h2 {{ color: #58a6ff; margin: 30px 0 15px; font-size: 20px; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
    .subtitle {{ color: #8b949e; margin-bottom: 30px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }}
    .card .label {{ color: #8b949e; font-size: 13px; margin-bottom: 5px; }}
    .card .value {{ font-size: 28px; font-weight: bold; }}
    .positive {{ color: #3fb950; }}
    .negative {{ color: #f85149; }}
    .neutral {{ color: #d29922; }}
    table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #21262d; }}
    th {{ background: #161b22; color: #8b949e; font-size: 13px; }}
    tr:hover {{ background: #161b22; }}
    .chart-container {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin: 20px 0; }}
    .bar {{ display: inline-block; height: 20px; border-radius: 3px; margin: 2px 0; }}
    .bar-positive {{ background: #238636; }}
    .bar-negative {{ background: #da3633; }}
    .bar-label {{ display: inline-block; width: 90px; font-size: 12px; }}
    .bar-value {{ display: inline-block; width: 60px; font-size: 12px; text-align: right; }}
    .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #30363d; color: #8b949e; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
    <h1>Quantum Flow 백테스트 리포트</h1>
    <div class="subtitle">
        기간: {data['start_date']} ~ {data['end_date']} |
        테스트일: {data['test_dates']}일 |
        생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}
    </div>

    <h2>핵심 성과 지표</h2>
    <div class="grid">
        <div class="card">
            <div class="label">평균 수익률 (5일)</div>
            <div class="value {'positive' if data['avg_return'] > 0 else 'negative'}">
                {data['avg_return']:+.2f}%
            </div>
        </div>
        <div class="card">
            <div class="label">벤치마크 수익률</div>
            <div class="value {'positive' if data['avg_benchmark'] > 0 else 'negative'}">
                {data['avg_benchmark']:+.2f}%
            </div>
        </div>
        <div class="card">
            <div class="label">초과 수익률</div>
            <div class="value {'positive' if data['avg_excess'] > 0 else 'negative'}">
                {data['avg_excess']:+.2f}%
            </div>
        </div>
        <div class="card">
            <div class="label">일별 승률</div>
            <div class="value {'positive' if data['hit_rate'] > 50 else 'negative'}">
                {data['hit_rate']:.1f}%
            </div>
        </div>
        <div class="card">
            <div class="label">종목별 승률</div>
            <div class="value {'positive' if stock_hit_rate > 50 else 'negative'}">
                {stock_hit_rate:.1f}%
            </div>
        </div>
        <div class="card">
            <div class="label">총 선정 종목</div>
            <div class="value neutral">{data['total_stocks']}개</div>
        </div>
        <div class="card">
            <div class="label">최대 수익일</div>
            <div class="value positive">{max_return:+.2f}%</div>
        </div>
        <div class="card">
            <div class="label">최대 손실일</div>
            <div class="value negative">{min_return:+.2f}%</div>
        </div>
    </div>

    <h2>날짜별 수익률 차트</h2>
    <div class="chart-container">
"""

    # 바 차트
    max_abs = max(abs(max_return), abs(min_return), 1)
    for d in sorted(valid_days, key=lambda x: x["date"]):
        width = abs(d["avg_return"]) / max_abs * 300
        bar_class = "bar-positive" if d["avg_return"] > 0 else "bar-negative"
        ret_class = "positive" if d["avg_return"] > 0 else "negative"
        html += f"""        <div>
            <span class="bar-label">{d['date'][5:]}</span>
            <span class="bar {bar_class}" style="width:{max(width,2):.0f}px"></span>
            <span class="bar-value {ret_class}">{d['avg_return']:+.2f}%</span>
        </div>\n"""

    html += """    </div>

    <h2>날짜별 상세 결과</h2>
    <table>
        <tr>
            <th>날짜</th><th>Risk</th><th>신뢰도</th>
            <th>후보</th><th>선정</th><th>평균수익</th>
            <th>벤치마크</th><th>초과수익</th>
        </tr>
"""

    for d in sorted(data["day_results"], key=lambda x: x["date"]):
        ret_class = "positive" if d["avg_return"] > 0 else "negative" if d["avg_return"] < 0 else ""
        exc_class = "positive" if d["excess_return"] > 0 else "negative" if d["excess_return"] < 0 else ""
        html += f"""        <tr>
            <td>{d['date']}</td>
            <td>{d['macro_risk']}</td>
            <td>{d['macro_confidence']}%</td>
            <td>{d['candidates_count']}</td>
            <td>{d['selected_count']}</td>
            <td class="{ret_class}">{d['avg_return']:+.2f}%</td>
            <td>{d['benchmark_return']:+.2f}%</td>
            <td class="{exc_class}">{d['excess_return']:+.2f}%</td>
        </tr>\n"""

    html += """    </table>

    <h2>선정 종목 상세</h2>
    <table>
        <tr>
            <th>테스트일</th><th>종목코드</th><th>진입가</th><th>5일 수익률</th>
        </tr>
"""

    for s in sorted(all_stocks, key=lambda x: x["test_date"]):
        ret_class = "positive" if s["return_pct"] > 0 else "negative"
        html += f"""        <tr>
            <td>{s['test_date']}</td>
            <td>{s['code']}</td>
            <td>{s.get('entry_price', 0):,.0f}</td>
            <td class="{ret_class}">{s['return_pct']:+.2f}%</td>
        </tr>\n"""

    html += f"""    </table>

    <div class="footer">
        Quantum Flow Backtest Report | Generated by backtest engine |
        승/패: {positive_days}/{negative_days}일 |
        종목 승/패: {positive_stocks}/{negative_stocks}개
    </div>
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"HTML 리포트 생성: {output_path}")
    return output_path


def print_summary(result):
    """콘솔 요약 출력"""
    if hasattr(result, '__dataclass_fields__'):
        data = asdict(result)
    else:
        data = result

    print(f"\n{'═' * 55}")
    print(f"  Quantum Flow 백테스트 요약")
    print(f"{'═' * 55}")
    print(f"  기간: {data['start_date']} ~ {data['end_date']}")
    print(f"  테스트일: {data['test_dates']}일")
    print(f"{'─' * 55}")
    print(f"  📈 평균 수익률:  {data['avg_return']:+.2f}%")
    print(f"  📊 벤치마크:     {data['avg_benchmark']:+.2f}%")
    print(f"  ✨ 초과 수익률:  {data['avg_excess']:+.2f}%")
    print(f"  🎯 승률:         {data['hit_rate']:.1f}%")
    print(f"  📋 총 선정종목:  {data['total_stocks']}개")
    print(f"  ⚠️  Risk OFF:    {data['risk_off_count']}일")
    print(f"{'═' * 55}\n")
