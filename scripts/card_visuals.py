"""Generate the three 'What it catches' header visuals (360x190, no text) and inline them into landing.html.

Style: 3-6 shapes per card, depth only from opacity gradients, lines fade at both ends,
one accent element per card, a soft radial halo only behind a key node, bottom fade into the card.
Palette (light theme): ink #09234F at low opacity, accent #384CE3, background #F3F5FB -> #FFFFFF.
"""
import re, pathlib
INK, ACC = "#09234F", "#384CE3"
W, H = 360, 190
_n = [0]
def uid(p):
    _n[0] += 1; return f"{p}{_n[0]}"
def lin(x1, y1, x2, y2, stops):
    i = uid("l")
    s = "".join(f'<stop offset="{o}" stop-color="{c}" stop-opacity="{a}"/>' for o, c, a in stops)
    return i, f'<linearGradient id="{i}" gradientUnits="userSpaceOnUse" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">{s}</linearGradient>'
def rad(cx, cy, r, stops):
    i = uid("r")
    s = "".join(f'<stop offset="{o}" stop-color="{c}" stop-opacity="{a}"/>' for o, c, a in stops)
    return i, f'<radialGradient id="{i}" gradientUnits="userSpaceOnUse" cx="{cx}" cy="{cy}" r="{r}">{s}</radialGradient>'
def fade_line(x1, y1, x2, y2, color=INK, peak=0.35):
    """gradient along the path: 0 -> peak -> peak*2.5 -> 0"""
    return lin(x1, y1, x2, y2, [(0, color, 0), (.25, color, peak), (.6, color, min(1, peak * 2.5)), (1, color, 0)])
def glow(cx, cy, r=26, a=.45):
    i, d = rad(cx, cy, r, [(0, ACC, a), (1, ACC, 0)])
    return d, f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#{i})"/>'
def node(cx, cy, r=4.5, accent=True):
    if accent:
        d, g = glow(cx, cy, r * 6)
        return d, g + f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{ACC}"/>'
    return "", f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{INK}" stroke-opacity=".35" stroke-width="1.4"/>'
def frame(defs, body):
    bg, bgd = rad(180, 70, 190, [(0, "#F1F4FC", 1), (1, "#FFFFFF", 1)])
    fd, fdd = lin(0, 0, 0, H, [(0, "#FFFFFF", 0), (.62, "#FFFFFF", 0), (1, "#FFFFFF", 1)])
    return (f'<svg class="ill" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">'
            f'<defs>{bgd}{fdd}{defs}</defs><rect width="{W}" height="{H}" fill="url(#{bg})"/>{body}'
            f'<rect width="{W}" height="{H}" fill="url(#{fd})"/></svg>')

# 1 Look-alike vendors: one source fans out to four look-alikes, only one path is the real vendor
def v1():
    defs, body = "", ""
    sx, sy = 70, 78
    ends = [(290, 30), (300, 64), (296, 98), (284, 130)]
    for k, (ex, ey) in enumerate(ends):
        lit = k == 1
        i, d = fade_line(sx, sy, ex, ey, ACC if lit else INK, .5 if lit else .18)
        defs += d
        body += f'<path d="M{sx} {sy} C{sx+90} {sy}, {ex-110} {ey}, {ex} {ey}" fill="none" stroke="url(#{i})" stroke-width="{1.8 if lit else 1.3}"/>'
        d2, n = node(ex, ey, 4 if lit else 3.2, accent=lit); defs += d2; body += n
    d3, n = node(sx, sy, 5); defs += d3; body += n
    return frame(defs, body)

# 2 Duplicates and diversion: one invoice outline, its faint duplicate behind, a single check: paid once
def v2():
    defs = ""
    fi, fd = lin(0, 28, 0, 140, [(0, INK, .10), (1, INK, 0)]); defs += fd
    si, sd = lin(0, 28, 0, 140, [(0, INK, .4), (1, INK, .05)]); defs += sd
    di, dd = lin(0, 40, 0, 150, [(0, INK, .18), (1, INK, 0)]); defs += dd
    gd, g = glow(232, 74, 44, .4); defs += gd
    body = (f'<rect x="150" y="40" width="96" height="112" rx="8" fill="none" stroke="url(#{di})" stroke-width="1.3" stroke-dasharray="4 4"/>'
            f'<rect x="126" y="28" width="96" height="112" rx="8" fill="url(#{fi})" stroke="url(#{si})" stroke-width="1.4"/>'
            + g +
            f'<path d="M214 74l12 12 24-26" fill="none" stroke="{ACC}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>')
    return frame(defs, body)

# 3 Directors, namesakes cleared: branches from one person; only the branch that names the company ends in a lit node
def v3():
    defs, body = "", ""
    ox, oy = 78, 96
    br = [(286, 38, True), (300, 100, False), (270, 150, False)]
    for ex, ey, lit in br:
        i, d = fade_line(ox, oy, ex, ey, ACC if lit else INK, .55 if lit else .16); defs += d
        body += f'<path d="M{ox} {oy} C{ox+80} {oy}, {ex-120} {ey}, {ex} {ey}" fill="none" stroke="url(#{i})" stroke-width="{1.8 if lit else 1.3}"/>'
    d1, n1 = node(286, 38, 7); defs += d1; body += n1
    body += f'<circle cx="{ox}" cy="{oy}" r="5" fill="{INK}" fill-opacity=".85"/>'
    return frame(defs, body)

if __name__ == "__main__":
    p = pathlib.Path(__file__).resolve().parents[1] / "vendorcheck" / "landing.html"
    s = p.read_text()
    olds = re.findall(r'<svg class="ill".*?</svg>', s, flags=re.S)
    assert len(olds) == 3, len(olds)
    for o, n in zip(olds, [v1(), v2(), v3()]):
        s = s.replace(o, n, 1)
    p.write_text(s)
    print("ok")
