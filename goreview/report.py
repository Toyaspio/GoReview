import asyncio
import base64
from html import escape
import json
from pathlib import Path
from sgfmill import sgf
from .errors import ReviewError
from .metrics import move_metrics
from .sgf_parser import replay, point_from_gtp, COLS


def report_mistakes(analysis):
    return sorted(analysis['mistakes'],
                  key=lambda row: (row['score_loss'], row['winrate_loss']), reverse=True)[:3]


def mistake_label(item):
    label = item.get('grade', '失误').split('（', 1)[0]
    return '失误' if label == '候选失误' else label


def mistake_badge(item):
    label = mistake_label(item)
    severity = 'danger' if label in ('严重失误', '关键胜负手') else 'warning'
    return f'<span class="mistake-badge {severity}">{escape(label)}</span>'


def template_summary(record, analysis):
    rows = report_mistakes(analysis)
    lead = f'这盘共{record.total_moves}手，结果为{record.result or "未知"}。'
    if analysis['positions']:
        first,last=analysis['positions'][0],analysis['positions'][-1]
        lead += f'黑方胜率由{first["black_winrate"]*100:.1f}%变为{last["black_winrate"]*100:.1f}%。'
    if rows:
        row = rows[0]
        middle = (f'重点回看掉点最多的{len(rows)}手，其中第{row["move_number"]}手目差损失约'
                  f'{row["score_loss"]:.1f}目。对照实战{row["played"]}与推荐{row["recommended"]}，'
                  '结合图中的变化顺序，看看双方后续应对有何不同。')
    else:
        middle = '这盘未筛出明显失误。可以沿胜率曲线回看局势变化，留意优势如何建立、落子如何衔接。'
    return lead + middle + '复盘时先摆实战，再摆推荐变化，把关键选择前后的局面连起来看。'


def board_svg(board, size, *, marks=None, numbers=None, last=None):
    marks, numbers = marks or [], numbers or {}
    margin, step = 30, 400 / (size-1)
    xy = lambda p: (margin+p[1]*step, margin+(size-1-p[0])*step)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 460 460" role="img" aria-label="围棋局面">',
           '<rect width="460" height="460" rx="8" fill="#e4c48b"/>']
    for index in range(size):
        z = margin+index*step
        out.append(f'<path d="M30 {z} H430 M{z} 30 V430" stroke="#725b37" stroke-width=".8"/>')
        out.append(f'<text x="{z}" y="451" text-anchor="middle" font-size="12">{COLS[index]}</text>')
        out.append(f'<text x="12" y="{z+4}" text-anchor="middle" font-size="12">{size-index}</text>')
    for row in range(size):
        for col in range(size):
            colour = board.get(row, col)
            if not colour:
                continue
            x, y = xy((row, col))
            radius = min(step*.46, 12)
            out.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{"#171b20" if colour=="b" else "#fff"}" stroke="#4d4b44"/>')
            if (row, col) in numbers:
                out.append(f'<text x="{x}" y="{y+4}" text-anchor="middle" fill="{"white" if colour=="b" else "black"}" font-size="11">{numbers[row,col]}</text>')
            elif last == (row, col):
                out.append(f'<circle cx="{x}" cy="{y}" r="3" fill="#dd693b"/>')
    for point, colour, label in marks:
        if point is None:
            continue
        x, y = xy(point)
        out.append(f'<circle cx="{x}" cy="{y}" r="{min(step*.47,13)}" fill="none" stroke="{colour}" stroke-width="3"/>')
        out.append(f'<text x="{x+8}" y="{y-9}" fill="{colour}" font-size="13" font-weight="bold">{escape(label)}</text>')
    return ''.join(out)+'</svg>'


def diagrams(record, item, snapshots):
    board = snapshots[item['move_number']-1]
    actual = point_from_gtp(item['played'], record.board_size)
    best = point_from_gtp(item['recommended'], record.board_size)
    previous = board_svg(board, record.board_size,
                         marks=[(actual, '#b33a2f', '实'), (best, '#126bb1', '荐')])
    variation = board.copy()
    labels = {}
    colour = item['colour'].lower()
    ko = None
    last = None
    for i, move in enumerate(item['pv'][:8], 1):
        point = point_from_gtp(move, record.board_size)
        if point is not None:
            if point == ko or variation.get(*point):
                raise ReviewError('invalid_pv', 'AI 推荐变化含非法落子，无法生成准确棋盘。')
            ko = variation.play(*point, colour)
            if variation.get(*point) != colour:
                raise ReviewError('invalid_pv', 'AI 推荐变化出现自杀落子。')
            labels = {p: number for p, number in labels.items() if variation.get(*p)}
            labels[point] = i
        else:
            ko = None
        last = point
        colour = 'w' if colour == 'b' else 'b'
    following = board_svg(variation, record.board_size, numbers=labels, last=last)
    return previous, following


def html_report(record, analysis, summary, *, fixture=False):
    if len(analysis['positions']) != record.total_moves + 1:
        raise ReviewError('chart_incomplete', '胜率曲线缺少局面。')
    rows = report_mistakes(analysis)
    game = sgf.Sgf_game.from_string(record.sgf_content)
    _, snapshots, _, _ = replay(game)
    info = [('黑方', record.black_player), ('白方', record.white_player), ('结果', record.result),
            ('总手数', str(record.total_moves)), ('来源', '野狐围棋'),
            ('贴目', str(record.komi) + '目' if record.komi is not None else '未知')]
    prefix=analysis.get('scope')=='prefix_test'
    if prefix:
        info[3]=('本段 / 全局手数',f'{record.total_moves} / {analysis["original_total_moves"]}')
        info[5]=('模式','真实数据流程测试（非正式复盘）')
    cards = ''.join(f'<div><small>{escape(k)}</small><strong>{escape(v or "未知")}</strong></div>' for k,v in info)
    metrics = move_metrics(record, analysis['positions'])
    agreement_cards = ''
    for colour in ('B', 'W'):
        label = '黑方' if colour == 'B' else '白方'
        for key, title in (('weighted', '吻合度'), ('first_choice', '第一选点率')):
            stats = metrics[key][colour]
            rate = f'{stats["rate"] * 100:.1f}%' if stats['rate'] is not None else '暂无数据'
            detail = (f'首选命中 {stats["matched"]} / {stats["compared"]} 手' if key == 'first_choice'
                      else f'已统计 {stats["compared"]} 手')
            agreement_cards += f'<div><small>{label}{title}</small><strong>{rate}</strong><small>{detail}</small></div>'
    table = ''.join(f'<tr><td>{i}</td><td>{m["move_number"]}</td><td>{"黑" if m["colour"]=="B" else "白"}</td>'
                    f'<td>{escape(m["played"])}</td><td>{escape(m["recommended"])}</td>'
                    f'<td>{m["winrate_loss"]*100:.1f}百分点</td><td>{m["score_loss"]:.1f}目</td>'
                    f'<td>{mistake_badge(m)}</td></tr>' for i,m in enumerate(rows,1))
    boards_html = ''
    overview_html = ''
    for rank, item in enumerate(rows, 1):
        before, after = diagrams(record, item, snapshots)
        label = mistake_label(item)
        severity = 'danger' if label in ('严重失误', '关键胜负手') else 'warning'
        overview_html += (f'<article class="overview-card key" aria-label="关键失误 {rank} · 第{item["move_number"]}手">'
                          f'<div class="overview-head"><span class="turn-pill">{rank} · 第{item["move_number"]}手</span>'
                          f'<span>目差损失 <b class="loss">{item["score_loss"]:.1f}目</b></span></div>'
                          '<div class="boards"><div>'+before+
                          f'<p><b>{escape(item["played"])} → {escape(item["recommended"])}</b></p>'
                          f'<p><span class="mistake-badge {severity}">{escape(label)}</span></p>'
                          f'<small>{"黑方" if item["colour"]=="B" else "白方"} · 胜率下降 {item["winrate_loss"]*100:.1f} 个百分点</small>'
                          '<small>红圈为实战，蓝圈为推荐</small></div><div class="variation">'
                          '<h3>推荐变化</h3>'+after+
                          '<small>前八步 · 数字为落子顺序</small></div></div>'
                          f'<p class="pv-text">完整变化：{escape(" → ".join(item["pv"]) or "未提供")}</p></article>')
    points = analysis['positions']
    chart = {'turns': [p['turn'] for p in points],
             'black': [round(p['black_winrate']*100,3) for p in points],
             'white': [round(p['white_winrate']*100,3) for p in points],
             'scores': [p['black_score_lead'] for p in points],
             'visits': [p['visits'] for p in points],
             'marks': [{'coord':[m['move_number'], round(points[m['move_number']]['black_winrate']*100,3)],
                        'value':str(m['move_number'])} for m in rows]}
    data = json.dumps(chart, ensure_ascii=True).replace('<','\\u003c')
    echarts = (Path(__file__).parent / 'assets' / 'echarts.min.js').read_text(encoding='utf8')
    metric_json = json.dumps(metrics, ensure_ascii=True).replace('<', '\\u003c')
    metric_script = (Path(__file__).parent / 'assets' / 'metrics.js').read_text(encoding='utf8')
    dashboard_css = (Path(__file__).parent / 'assets' / 'dashboard.css').read_text(encoding='utf8')
    art = base64.b64encode((Path(__file__).parent / 'assets' / 'ayu-dashboard-reference.png').read_bytes()).decode('ascii')
    badge = '<div class="fixture">离线合成分析数据 · 仅用于版式与流程测试 · 不是本局真实 AI 复盘</div>' if fixture else ''
    if prefix:
        badge=f'<div class="fixture">真实野狐＋智子数据测试 · 仅分析前{record.total_moves}手 · 不代表整盘走势与恶手排名 · 贴目网页对照及正式视角待验收</div>'
        if not rows:
            sample=min(record.total_moves,6)
            position=analysis['positions'][sample-1]
            move=record.metadata['moves'][sample-1]
            item={'move_number':sample,'colour':move[0],'played':move[1],
                  'recommended':position['recommendation'],'pv':position['pv']}
            left,right=diagrams(record,item,snapshots)
            boards_html+='<section><h2>局面示例（不是恶手判定）</h2><div class="boards"><div>'+left+'</div><div>'+right+'</div></div><p>左：实战与推荐落点；右：实际引擎推荐变化。</p></section>'
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=1056">
<style>{dashboard_css}header.hero,.avatar,.summary-character{{background-image:url("data:image/png;base64,{art}")}}</style>
<main>{badge}<header class="hero"><h1>阿羽围棋复盘报告</h1><p class="header-credit">toya开发</p></header>
<section class="info"><span class="avatar" aria-hidden="true"></span>{cards}</section>
<section class="agreement"><h2 class="section-title"><span class="section-icon">◉</span>吻合度与第一选点率</h2><div class="metric-cards">{agreement_cards}</div>
<p class="metric-definition"><small>吻合度：实战候选搜索量占最高候选搜索量的平均比例　·　第一选点率：实战落子与 AI 首选一致的比例</small></p></section>
<section class="charts"><div class="chart-panel"><div id="metric-comparison"></div></div>
<div class="chart-panel"><h2 class="section-title"><span class="section-icon">✦</span>胜率走势</h2><div id="chart"></div><small>纵轴：胜率 / %　横轴：手数</small></div></section>
<section><h2 class="section-title"><span class="section-icon">◎</span>关键失误 · 掉点最多的三手</h2><div class="mistake-overview">{overview_html}</div><table class="mistake-table"><thead><tr><th>排名</th><th>手数</th><th>执子</th><th>实战</th><th>推荐</th><th>胜率下降</th><th>目差损失</th><th>评价</th></tr></thead><tbody>{table or '<tr><td colspan="8">未筛出明显失误</td></tr>'}</tbody></table></section>
{boards_html}<section class="summary-card"><h2 class="section-title"><span class="section-icon">✧</span>阿羽的复盘小结</h2><div class="summary">{escape(summary)}</div><span class="summary-character" aria-hidden="true"></span></section>
<footer>分析耗时 {analysis['elapsed_seconds']:.1f} 秒 · 来源棋局 {escape(record.source_game_id)}　｜　欢迎加入围棋群交流：315910992</footer></main>
<script>{echarts}</script><script>const metricData={metric_json};{metric_script}</script>
<script>const d={data};const chart=echarts.init(document.getElementById('chart'),null,{{renderer:'svg'}});chart.setOption({{animation:false,legend:{{data:['黑方'],selected:{{'黑方':true}}}},tooltip:{{trigger:'axis'}},grid:{{left:48,right:24,top:40,bottom:35}},xAxis:{{type:'category',data:d.turns}},yAxis:{{type:'value',min:0,max:100}},series:[{{name:'黑方',type:'line',data:d.black,showSymbol:false,itemStyle:{{color:'#286773'}},lineStyle:{{color:'#286773',width:3}},markPoint:{{symbolSize:34,data:d.marks}}}}]}});window.__reportReady=true;</script></html>'''


class Renderer:
    def __init__(self, directory, *, executable_path=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.executable_path = executable_path
        self.lock = asyncio.Semaphore(1)

    async def render(self, job_id, record, analysis, summary, *, fixture=False):
        from playwright.async_api import async_playwright
        document = html_report(record, analysis, summary, fixture=fixture)
        html_path = self.directory / (job_id + '.html')
        image_path = self.directory / (job_id + '.png')
        pending_path = self.directory / (job_id + '.pending.png')
        async with self.lock, asyncio.timeout(75):
            async with async_playwright() as p:
                browser = await p.chromium.launch(**({'executable_path': self.executable_path} if self.executable_path else {}))
                try:
                    context = await browser.new_context(viewport={'width':1056, 'height':900}, device_scale_factor=1)
                    await context.route('**/*', lambda route: route.abort())
                    page = await context.new_page()
                    await page.set_content(document, wait_until='load', timeout=15000)
                    await page.wait_for_function('window.__reportReady === true', timeout=15000)
                    await page.evaluate('document.fonts.ready')
                    await page.evaluate('''async () => {
                        const nodes=['header.hero','.avatar','.summary-character'];
                        const urls=nodes.map(s=>getComputedStyle(document.querySelector(s)).backgroundImage);
                        if(urls.some(s=>!s.startsWith('url("data:image/png;base64,')))
                            throw new Error('report_art_missing');
                        const picture=new Image();picture.src=urls[0].slice(5,-2);await picture.decode();
                    }''')
                    height = await page.evaluate('document.documentElement.scrollHeight')
                    if height > 16000:
                        raise ReviewError('report_size', '报告高度超过上限，停止截图以免裁切。')
                    await page.screenshot(path=str(pending_path), full_page=True, timeout=15000)
                    pending_path.replace(image_path)
                    html_path.write_text(document, encoding='utf8')
                finally:
                    await browser.close()
        return image_path
