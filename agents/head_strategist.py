# head_strategist.py â í¤ë ì ëµê° ìì´ì í¸ (Agent 3)
# ìµì¢ ë§ ë§¤ ê²°ì  + í¬í¸í´ë¦¬ì¤ ê´ë¦¬ + í¬ì§ì ì¬ì´ì§
# stock_evalì position_pct + ë§¤í¬ë¡ ì ëµì ì¢í©íì¬ ìµì¢ ì£¼ë¬¸ ê²°ì 

import asyncio
import logging
from datetime import datetime

from shared_state import (
    get_state, set_state, get_positions,
    add_position, remove_position, add_to_blacklist,
)
from config.settings import (
    MAX_POSITIONS, POSITION_SIZE_RATIO,
    DAILY_LOSS_LIMIT, RECOVERY_POSITION_RATIO,
)

from tools.trade_logger import log_trade, log_signal, log_risk_event

logger = logging.getLogger("head_strategist")


class HeadStrategist:
    """í¤ë ì ëµê° â ìµì¢ ë§ ë§¤ ê²°ì  ë° í¨í¸í´ë¦¬ì¤ ê´ë¦¬"""

    def __init__(self):
        self.name = "Head Strategist"

    async def run(self) -> dict:
        """
        ë¹ëê¸° ì¤í:
        1) Risk-OFF / ì¼ì¼ ìì¤ íë ì²´í¬
        2) watch_listìì ë§¤ì íë³´ íì¸
        3) í¬ì§ì ì¬ì´ì§ (ë§¤í¬ë¡ + stock_eval ë°ì)
        4) ê¸°ì¡´ í¬ì§ì ê´ë¦¬ (ì¶ê°ë§¤ì / ì²­ì° íë¨)
        """
        print(f"\n{'='*50}")
        print(f"  [{self.name}] ì ëµ ë¶ì ìì")
        print(f"{'='*50}")

        actions_taken = []

        # 1) Risk ì²´í¬
        risk_off = get_state("risk_off")
        daily_loss = get_state("daily_loss") or 0.0
        risk_params = get_state("risk_params") or {}
        risk_level = risk_params.get("risk_level", "NORMAL")

        if risk_off:
            print(f"  â Risk-OFF â ì ì²´ ë§¤ë§¤ ì¤ë¨")
            if risk_params.get("emergency_liquidate"):
                actions = self._emergency_liquidate()
                actions_taken.extend(actions)
            return {
                "status": "risk_off",
                "actions": actions_taken,
                "message": "Risk-OFF: ë§¤ë§¤ ì¤ë¨",
            }

        if daily_loss <= DAILY_LOSS_LIMIT:
            print(f"  â ì¼ì¼ ìì¤ íë ëë¬: {daily_loss:.2%}")
            log_risk_event("DAILY_LOSS_LIMIT", level="HIGH",
                           message=f"ì¼ì¼ ìì¤ íë ëë¬: {daily_loss:.2%}")
            return {
                "status": "loss_limit",
                "actions": [],
                "message": f"ì¼ì¼ ìì¤ íë {daily_loss:.2%}",
            }

        # 2) ë§¤í¬ë¡ ì ëµ íì¸
        macro = get_state("macro_result") or {}
        macro_position_pct = macro.get("position_size_pct", 0.5)
        strategy = macro.get("strategy", "ì¤ë¦½")

        print(f"\n  ë§¤í¬ë¡ ì ëµ: {strategy}")
        print(f"  ë§¤í¬ë¡ í¬ì§ì ë¹ì¤: {macro_position_pct*100:.0f}%")
        print(f"  ë¦¬ì¤í¬ ë ë²¨: {risk_level}")

        # 3) íì¬ í¬ì§ì íì¸
        positions = get_positions()
        current_count = len(positions)
        print(f"  íì¬ ë³´ì : {current_count}/{MAX_POSITIONS}ì¢ëª©")

        # 4) ì ê· ë§¤ì ê²í 
        if current_count < MAX_POSITIONS:
            watch_list = get_state("watch_list") or []
            scanner_result = get_state("scanner_result") or {}
            selected = scanner_result.get("selected", [])

            # selectedìì code â ìì¸ ì ë³´ ë§¤í
            selected_map = {}
            for s in selected:
                if isinstance(s, dict):
                    selected_map[s.get("code", "")] = s

            for code in watch_list:
                if current_count >= MAX_POSITIONS:
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="max_positions")
                    break
                if code in positions:
                    continue  # ì´ë¯¸ ë³´ì 
                blacklist = get_state("blacklist") or []
                if code in blacklist:
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="blacklisted")
                    continue  # ë¸ëë¦¬ì¤í¸

                # í¬ì§ì ì¬ì´ì¦ ê²°ì 
                info = selected_map.get(code, {})
                eval_pct = info.get("position_pct", 0.5)

                # ìµì¢ í¬ì§ì = ê¸°ë³¸ë¹ì x ë§¤í¬ë¡ë¹ì¤ x íê°ë¹ì¤
                final_pct = POSITION_SIZE_RATIO * macro_position_pct * eval_pct

                # [기능3] Recovery 재진입 시 포지션 축소
                recovery_state = get_state("recovery_state")
                if recovery_state == "RECOVERED":
                    final_pct *= RECOVERY_POSITION_RATIO


                # ë°©ì´ì  ì ë´ì´ë©´ ì¶ê° ì¶ì
                if strategy == "ë°©ì´ì ":
                    final_pct *= 0.5
                elif strategy == "ê³µê²©ì ":
                    final_pct *= 1.2

                final_pct = min(final_pct, POSITION_SIZE_RATIO)  # ìí

                if final_pct < 0.02:
                    print(f"    {code}: í¬ì§ì ëë¬´ ìì ({final_pct:.1%}) â ì¤íµ")
                    log_signal(code, "BUY_SIGNAL", executed=False,
                               skip_reason="position_too_small",
                               eval_grade=info.get("eval_grade", "?"))
                    continue

                action = {
                    "type": "BUY",
                    "code": code,
                    "position_pct": round(final_pct, 3),
                    "eval_grade": info.get("eval_grade", "?"),
                    "reason": f"watch_list ë§¤ì ({strategy}, {final_pct:.1%})",
                    "timestamp": datetime.now().isoformat(),
                }
                actions_taken.append(action)
                print(f"    ð ë§¤ì ê²°ì : {code} ({final_pct:.1%})")

                # ë§ ë§¤ ê¸°ë¡ ì ì¥
                log_trade("BUY", code,
                          position_pct=round(final_pct, 3),
                          eval_grade=info.get("eval_grade", "?"),
                          eval_score=info.get("eval_score"),
                          sector=info.get("sector", ""),
                          strategy=strategy,
                          reason=action["reason"])
                log_signal(code, "BUY_SIGNAL", executed=True,
                           eval_grade=info.get("eval_grade", "?"),
                           eval_score=info.get("eval_score"))

                # ìì í¬ì§ì ë±ë¡
                add_position(code, {
                    "entry_pct": final_pct,
                    "eval_grade": info.get("eval_grade", "?"),
                    "sector": info.get("sector", ""),
                    "entry_time": datetime.now().isoformat(),
                    "pyramiding_done": False,
                    "pyramid_count": 0,
                    "entry_atr": info.get("entry_atr", 0),
                })
                current_count += 1

        # 5) ê¸°ì¡´ í¬ì§ì ê´ë¦¬ (ì¶ê°ë§¤ì íë¨)
        if risk_params.get("pyramiding_allowed", True):
            for code, pos_data in positions.items():
                if pos_data.get("pyramiding_done"):
                    continue
                # ì¶ê°ë§¤ì ì¡°ê±´ì ì¤ìê° ê°ê²© íì â ì¬ê¸°ìë êµ¬ì¡°ë§ ì¤ë¹
                # market_watcherìì ì¤ìê° ê°ê²© + í¸ë¦¬ê±° ë°ë ì í¸ì¶
        result = {
            "status": "success",
            "actions": actions_taken,
            "positions_count": current_count,
            "strategy": strategy,
            "message": f"{len(actions_taken)}ê±´ ë§¤ë§¤ ê²°ì ",
            "timestamp": datetime.now().isoformat(),
        }

        set_state("strategist_result", result)

        print(f"\n  â [{self.name}] ì ëµ ìë£")
        print(f"     ë§ ë§¤ ê²°ì : {len(actions_taken)}ê±´")
        print(f"     ë³´ì  ì¢ëª©: {current_count}/{MAX_POSITIONS}")

        return result

    def _emergency_liquidate(self) -> list:
        """ê¸´ê¸ ì ë ì²­ì°"""
        positions = get_positions()
        actions = []
        log_risk_event("EMERGENCY_LIQUIDATE",
                       level="CRITICAL",
                       message=f"{len(positions)}ì¢êª© ê¸´ê¸ ì ë ì²­ì°")
        for code in list(positions.keys()):
            actions.append({
                "type": "SELL_ALL",
                "code": code,
                "reason": "ê¸´ê¸ ì ë ì²­ì° (CRITICAL)",
                "timestamp": datetime.now().isoformat(),
            })
            log_trade("FORCE_CLOSE", code,
                      reason="ê¸´ê¸ ì ë ì²­ì° (CRITICAL)",
                      position_pct=positions[code].get("entry_pct", 0))
            remove_position(code)
            add_to_blacklist(code)
            print(f"    ð¨ ê¸´ê¸ ì²­ì°: {code}")
        return actions


async def head_strategist_run() -> dict:
    """í¤ë ì ëµê° ì¤í í¨ì â async defë¡ ì ì"""
    strategist = HeadStrategist()
    return await strategist.run()
