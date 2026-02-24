"""
run_backtest.py — 백테스트 실행 진입점

사용법:
  python -m backtest.run_backtest --csv-dir ./collected_data --dates 10
  python -m backtest.run_backtest --csv-dir ./collected_data --dates 50 --llm
  python -m backtest.run_backtest --csv-dir ./collected_data --dates 5 --no-news --no-dart

기본값: 실제 뉴스 크롤링 ON, DART 공시 ON, LLM OFF (규칙 기반)
"""

import os
import sys
import argparse
import logging

# 프로젝트 루트
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest.engine import BacktestEngine
from backtest.report import generate_html_report, print_summary


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="Quantum Flow 백테스트")
    parser.add_argument("--csv-dir", default="./collected_data",
                        help="키움 수집 데이터 폴더 (기본: ./collected_data)")
    parser.add_argument("--dates", type=int, default=10,
                        help="테스트할 랜덤 날짜 수 (기본: 10)")
    parser.add_argument("--forward-days", type=int, default=5,
                        help="수익률 측정 기간 (기본: 5일)")
    parser.add_argument("--top-n", type=int, default=50,
                        help="거래량 상위 종목 수 (기본: 50)")
    parser.add_argument("--max-select", type=int, default=10,
                        help="최종 선정 종목 수 (기본: 10)")
    parser.add_argument("--llm", action="store_true",
                        help="실제 Claude API 사용 (비용 발생)")
    parser.add_argument("--no-news", action="store_true",
                        help="네이버 뉴스 크롤링 비활성화 (가상 뉴스)")
    parser.add_argument("--no-dart", action="store_true",
                        help="DART 공시 조회 비활성화")
    parser.add_argument("--seed", type=int, default=42,
                        help="랜덤 시드 (기본: 42)")
    parser.add_argument("--output", default="backtest/results",
                        help="결과 출력 폴더")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    use_news = not args.no_news
    use_dart = not args.no_dart

    print(f"\n{'═' * 55}")
    print(f"  Quantum Flow 백테스트 시작")
    print(f"{'═' * 55}")
    print(f"  CSV 폴더: {args.csv_dir}")
    print(f"  테스트 날짜: {args.dates}일")
    print(f"  수익률 측정: {args.forward_days}일")
    print(f"  LLM 사용: {'예 (Claude API)' if args.llm else '아니오 (규칙 기반)'}")
    print(f"  뉴스 크롤링: {'예 (네이버)' if use_news else '아니오 (가상 뉴스)'}")
    print(f"  DART 공시: {'예' if use_dart else '아니오'}")
    print(f"{'═' * 55}\n")

    # 엔진 초기화 + 실행
    engine = BacktestEngine(
        csv_dir=args.csv_dir,
        use_real_llm=args.llm,
        use_real_news=use_news,
        use_dart=use_dart,
        forward_days=args.forward_days,
        top_n=args.top_n,
        max_select=args.max_select,
        seed=args.seed,
    )

    result = engine.run(n_dates=args.dates)

    # 결과 저장
    json_path = os.path.join(args.output, "result.json")
    html_path = os.path.join(args.output, "report.html")

    engine.save_result(result, json_path)
    generate_html_report(result, html_path)
    print_summary(result)

    print(f"  JSON: {os.path.abspath(json_path)}")
    print(f"  HTML: {os.path.abspath(html_path)}")


if __name__ == "__main__":
    main()
