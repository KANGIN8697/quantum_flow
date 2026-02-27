"""
QUANTUM FLOW 공용 유틸리티 함수
- safe_float: float() 안전 래퍼
- safe_int: int() 안전 래퍼
"""
import math


def safe_float(val, default=0.0):
    """float() 안전 래퍼 - pandas Series, NaN, 빈 문자열 등 처리"""
    try:
        if hasattr(val, 'iloc'):
            val = val.iloc[0] if len(val) > 0 else default
        if val is None or val == '':
            return default
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (ValueError, TypeError, IndexError):
        return default


def safe_int(val, default=0):
    """int() 안전 래퍼 - pandas Series, NaN, 빈 문자열 등 처리"""
    try:
        if hasattr(val, 'iloc'):
            val = val.iloc[0] if len(val) > 0 else default
        if val is None or val == '':
            return default
        if isinstance(val, float) and math.isnan(val):
            return default
        return int(float(val))
    except (ValueError, TypeError, IndexError):
        return default
