"""Browser coordinates to a store-search landmark.

The upstream store search only accepts city + keyword, so coordinates are
reverse-geocoded first. Coordinates leave the machine (to AMap) only after the
user opted in during the conversation and then allowed it on the locate page.
"""
import math
import os

import httpx

_A = 6378245.0
_EE = 0.00669342162296594323


def amap_key():
    return os.environ.get('AMAP_KEY', '').strip()


def _outside_china(lat, lng):
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _lat_offset(x, y):
    r = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    r += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi))*2/3
    r += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30))*2/3
    return r


def _lng_offset(x, y):
    r = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    r += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi))*2/3
    r += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi))*2/3
    return r


def wgs84_to_gcj02(lat, lng):
    """Browsers report WGS-84; Chinese map services expect GCJ-02 (offset of a few hundred metres)."""
    if _outside_china(lat, lng):
        return lat, lng
    dlat, dlng = _lat_offset(lng-105, lat-35), _lng_offset(lng-105, lat-35)
    rad = lat/180*math.pi
    magic = 1 - _EE*math.sin(rad)**2
    sq = math.sqrt(magic)
    dlat = dlat*180/((_A*(1-_EE))/(magic*sq)*math.pi)
    dlng = dlng*180/(_A/sq*math.cos(rad)*math.pi)
    return lat+dlat, lng+dlng


def _text(value):
    # AMap encodes empty fields as [].
    return value.strip() if isinstance(value, str) else ''


def landmark(regeocode):
    """City and nearby-place keyword for query-nearby-stores."""
    comp = regeocode.get('addressComponent') or {}
    city = _text(comp.get('city')) or _text(comp.get('province'))
    pois = [p for p in regeocode.get('pois') or [] if _text(p.get('name'))]
    street = comp.get('streetNumber') or {}
    keyword = (_text(pois[0]['name']) if pois else '') or (_text(street.get('street'))+_text(street.get('number'))) or _text(comp.get('township')) or _text(comp.get('district'))
    if not city or not keyword:
        raise ValueError('这个位置没换算出附近地名。')
    return city, keyword


async def reverse_geocode(lat, lng):
    key = amap_key()
    if not key:
        raise ValueError('本机没有配置定位服务。')
    lat, lng = wgs84_to_gcj02(lat, lng)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get('https://restapi.amap.com/v3/geocode/regeo',
                params={'key': key, 'location': f'{lng:.6f},{lat:.6f}', 'extensions': 'all', 'radius': 500})
            data = r.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError('定位服务暂时连不上。') from None
    if str(data.get('status')) != '1' or not data.get('regeocode'):
        raise ValueError('定位服务没有返回地名。')
    return landmark(data['regeocode'])
