"""Copy boundary for eater-facing text; structured model fields stay unchanged."""
import re

BANNED=('MCP','上游','引擎','白名单','核价依据','参数','接口','session','plan_id','addressId','token','有界搜索')
REPLACEMENTS={
    '有界搜索':'这次比较', '核价依据':'刚才的报价','白名单确认句':'标准确认句',
    '白名单':'可选范围','上游':'麦当劳','引擎':'点餐助手','参数':'订单信息',
    '接口':'服务','MCP':'点餐服务','session_id':'这次点餐','session':'这次点餐',
    'plan_id':'这份报价','addressId':'配送地址','confirm_token':'确认信息','token':'Token',
}


def user_text(value):
    # Preserve quoted official wording verbatim, including validation terminology.
    before,marker,quote=str(value).partition('麦当劳原话：')
    # URLs (including McDonald's /mcp/ payment paths), file paths and code identifiers
    # are data, not prose. A copy pass must never mutate a destination.
    parts=re.split(r'(https?://[^\s<>]+|[A-Za-z]:[\\/][^\s<>]+|`[^`]*`)',before)
    for i,part in enumerate(parts):
        if i%2:continue
        for old,new in REPLACEMENTS.items():
            pattern=re.escape(old)
            if old.isascii():pattern=r'(?<![A-Za-z0-9_-])'+pattern+r'(?![A-Za-z0-9_-])'
            part=re.sub(pattern,lambda _:new,part,flags=re.I)
        parts[i]=part
    before=''.join(parts)
    return before+marker+quote
