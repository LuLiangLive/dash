"""
api/export.py —— 数据导出API

从 api/advanced.py 拆分（v2.5.5架构重构）
负责：自选列表导出、持仓数据导出、榜单数据导出
"""
import csv
import io
import datetime
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import db

router = APIRouter(prefix='/api/advanced', tags=['数据导出'])


def get_db():
    """获取数据库连接"""
    return db.get_conn()


# ============================================================
# 数据导出API
# ============================================================

@router.get('/export/watchlist', summary='导出自选基金列表', responses={200: {'description': 'CSV/XLSX文件下载'}})
async def export_watchlist(format: str = 'csv'):
    """导出自选基金列表（支持xlsx/csv格式）"""
    db_conn = get_db()
    rows = db_conn.execute('''
        SELECT w.code, w.name, w.added_at, f.score, f.ad_score, f.earn_score,
               f.nav, f.nav_date, f.d1, f.d3, f.d7, f.d10, f.ms, f.reco
        FROM watchlist w
        LEFT JOIN funds f ON w.code = f.code
        ORDER BY w.position
    ''').fetchall()

    if format == 'xlsx':
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers

            wb = Workbook()
            ws = wb.active
            ws.title = '自选基金列表'

            header_font = Font(bold=True, color='FFFFFF', size=11)
            header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
            header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
            thin_border = Border(
                left=Side(style='thin'), right=Side(style='thin'),
                top=Side(style='thin'), bottom=Side(style='thin')
            )
            up_font = Font(color='00B050')
            down_font = Font(color='FF0000')

            headers = ['基金代码', '基金名称', '添加时间', '综合分', '抗跌分', '收益分',
                       '最新净值', '净值日期', '日涨幅%', '3日涨幅%', '7日涨幅%', '10日涨幅%',
                       '动能状态', '推荐信号']
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = thin_border

            for row_idx, r in enumerate(rows, 2):
                values = [r['code'], r['name'], r['added_at'], r['score'], r['ad_score'],
                         r['earn_score'], r['nav'], r['nav_date'], r['d1'], r['d3'],
                         r['d7'], r['d10'], r['ms'], r['reco']]
                for col, val in enumerate(values, 1):
                    cell = ws.cell(row=row_idx, column=col, value=val)
                    cell.border = thin_border
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    # 涨跌幅列着色
                    if col in (9, 10, 11, 12) and val is not None:
                        cell.number_format = '0.00'
                        if val > 0:
                            cell.font = up_font
                        elif val < 0:
                            cell.font = down_font
                    # 评分列着色
                    if col in (4, 5, 6) and val is not None:
                        if val >= 80:
                            cell.font = Font(color='00B050', bold=True)
                        elif val >= 60:
                            cell.font = Font(color='FFC000')
                        else:
                            cell.font = Font(color='FF0000')

            # 列宽设置
            col_widths = [12, 30, 18, 8, 8, 8, 10, 12, 10, 10, 10, 10, 10, 10]
            for i, w in enumerate(col_widths, 1):
                ws.column_dimensions[chr(64 + i)].width = w

            # 冻结首行
            ws.freeze_panes = 'A2'

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)

            return StreamingResponse(
                iter([output.getvalue()]),
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition': f'attachment; filename=watchlist_{datetime.date.today()}.xlsx'}
            )
        except ImportError:
            pass

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['基金代码', '基金名称', '添加时间', '综合分', '抗跌分', '收益分',
                     '最新净值', '净值日期', '日涨幅', '3日涨幅', '7日涨幅', '10日涨幅',
                     '动能状态', '推荐信号'])
    for r in rows:
        writer.writerow([r['code'], r['name'], r['added_at'], r['score'], r['ad_score'],
                         r['earn_score'], r['nav'], r['nav_date'], r['d1'], r['d3'],
                         r['d7'], r['d10'], r['ms'], r['reco']])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=watchlist_{datetime.date.today()}.csv'}
    )


@router.get('/export/portfolio', summary='导出持仓收益数据', responses={200: {'description': 'CSV/XLSX文件下载（含持仓汇总和投资记录明细）'}})
async def export_portfolio(format: str = 'xlsx'):
    """导出持仓收益数据（支持xlsx/csv格式，含持仓汇总和投资记录明细）"""
    db_conn = get_db()
    # 导入持仓实时更新函数（从portfolio模块复用）
    from modules.portfolio.router import _update_portfolio_realtime
    _update_portfolio_realtime(db_conn)

    portfolios = db_conn.execute('SELECT * FROM portfolio ORDER BY total_amount DESC').fetchall()
    records = db_conn.execute('''
        SELECT * FROM investment_records
        ORDER BY code, date DESC, id DESC
    ''').fetchall()

    if format == 'xlsx':
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

            wb = Workbook()

            # Sheet1: 持仓汇总
            ws1 = wb.active
            ws1.title = '持仓汇总'

            header_font = Font(bold=True, color='FFFFFF')
            header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
            header_align = Alignment(horizontal='center', vertical='center')
            thin_border = Border(
                left=Side(style='thin'), right=Side(style='thin'),
                top=Side(style='thin'), bottom=Side(style='thin')
            )

            headers1 = ['基金代码', '基金名称', '持有份额', '平均成本', '当前净值', '净值日期',
                       '持仓金额', '浮动盈亏', '收益率']
            for col, header in enumerate(headers1, 1):
                cell = ws1.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = thin_border

            for row_idx, p in enumerate(portfolios, 2):
                values = [p['code'], p['name'], p['total_shares'], p['avg_cost'],
                         p['current_nav'], p['nav_date'], p['total_amount'],
                         p['total_profit'], p['profit_pct']]
                for col, val in enumerate(values, 1):
                    cell = ws1.cell(row=row_idx, column=col, value=val)
                    cell.border = thin_border
                    if col == 9 and val is not None:
                        cell.number_format = '0.00%'
                        if val > 0:
                            cell.font = Font(color='00B050')
                        elif val < 0:
                            cell.font = Font(color='FF0000')

            ws1.column_dimensions['A'].width = 12
            ws1.column_dimensions['B'].width = 30
            ws1.column_dimensions['C'].width = 12
            ws1.column_dimensions['D'].width = 12
            ws1.column_dimensions['E'].width = 12
            ws1.column_dimensions['F'].width = 12
            ws1.column_dimensions['G'].width = 12
            ws1.column_dimensions['H'].width = 12
            ws1.column_dimensions['I'].width = 10

            # Sheet2: 投资记录明细
            ws2 = wb.create_sheet('投资记录明细')

            headers2 = ['基金代码', '基金名称', '交易类型', '交易日期', '投资金额',
                       '确认份额', '成交净值', '手续费', '止盈目标', '备注', '来源']
            for col, header in enumerate(headers2, 1):
                cell = ws2.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = thin_border

            for row_idx, r in enumerate(records, 2):
                type_map = {'buy': '买入', 'sell': '卖出', 'dividend': '分红'}
                record_type = type_map.get(r['type'], r['type'])
                values = [r['code'], r['name'], record_type, r['date'], r['amount'],
                         r['shares'], r['nav'], r['fee'], r['profit_target'], r['note'], r['source']]
                for col, val in enumerate(values, 1):
                    cell = ws2.cell(row=row_idx, column=col, value=val)
                    cell.border = thin_border
                    if col == 3:
                        if record_type == '买入':
                            cell.font = Font(color='00B050')
                        elif record_type == '卖出':
                            cell.font = Font(color='FF0000')

            ws2.column_dimensions['A'].width = 12
            ws2.column_dimensions['B'].width = 30
            ws2.column_dimensions['C'].width = 10
            ws2.column_dimensions['D'].width = 12
            ws2.column_dimensions['E'].width = 12
            ws2.column_dimensions['F'].width = 12
            ws2.column_dimensions['G'].width = 12
            ws2.column_dimensions['H'].width = 10
            ws2.column_dimensions['I'].width = 10
            ws2.column_dimensions['J'].width = 20
            ws2.column_dimensions['K'].width = 10

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)

            return StreamingResponse(
                iter([output.getvalue()]),
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition': f'attachment; filename=portfolio_{datetime.date.today()}.xlsx'}
            )
        except ImportError:
            pass

    # CSV格式（默认）
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['基金代码', '基金名称', '持有份额', '平均成本', '当前净值', '净值日期',
                     '持仓金额', '浮动盈亏', '收益率'])
    for r in portfolios:
        writer.writerow([r['code'], r['name'], r['total_shares'], r['avg_cost'],
                         r['current_nav'], r['nav_date'], r['total_amount'],
                         r['total_profit'], r['profit_pct']])

    writer.writerow([])
    writer.writerow(['=== 投资记录明细 ==='])
    writer.writerow(['基金代码', '基金名称', '交易类型', '交易日期', '投资金额',
                     '确认份额', '成交净值', '手续费', '止盈目标', '备注', '来源'])
    for r in records:
        writer.writerow([r['code'], r['name'], r['type'], r['date'], r['amount'],
                         r['shares'], r['nav'], r['fee'], r['profit_target'], r['note'], r['source']])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=portfolio_{datetime.date.today()}.csv'}
    )


@router.get('/export/ranks', summary='导出榜单数据', responses={200: {'description': 'CSV/XLSX文件下载'}})
async def export_ranks(panel: str = 'day', format: str = 'csv'):
    """导出榜单数据（支持xlsx/csv格式）"""
    db_conn = get_db()
    latest_date = db_conn.execute('SELECT MAX(date) FROM rank_snapshots').fetchone()[0]
    rows = db_conn.execute('''
        SELECT * FROM rank_snapshots
        WHERE date = ? AND panel = ?
        ORDER BY sub, rank
    ''', (latest_date, panel)).fetchall()

    if format == 'xlsx':
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

            wb = Workbook()
            ws = wb.active
            ws.title = f'榜单_{panel}'

            header_font = Font(bold=True, color='FFFFFF', size=11)
            header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
            header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
            thin_border = Border(
                left=Side(style='thin'), right=Side(style='thin'),
                top=Side(style='thin'), bottom=Side(style='thin')
            )

            headers = ['日期', '榜单类型', '子榜单', '排名', '基金代码', '基金名称', '元数据']
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = thin_border

            for row_idx, r in enumerate(rows, 2):
                values = [r['date'], r['panel'], r['sub'], r['rank'], r['code'], r['name'], r['meta']]
                for col, val in enumerate(values, 1):
                    cell = ws.cell(row=row_idx, column=col, value=val)
                    cell.border = thin_border
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    # 排名前3高亮
                    if col == 4 and val is not None:
                        if val == 1:
                            cell.font = Font(color='FFD700', bold=True)
                        elif val == 2:
                            cell.font = Font(color='C0C0C0', bold=True)
                        elif val == 3:
                            cell.font = Font(color='CD7F32', bold=True)

            col_widths = [12, 10, 12, 8, 12, 30, 50]
            for i, w in enumerate(col_widths, 1):
                ws.column_dimensions[chr(64 + i)].width = w

            ws.freeze_panes = 'A2'

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)

            return StreamingResponse(
                iter([output.getvalue()]),
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition': f'attachment; filename=ranks_{panel}_{latest_date}.xlsx'}
            )
        except ImportError:
            pass

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['日期', '榜单类型', '子榜单', '排名', '基金代码', '基金名称', '元数据'])
    for r in rows:
        writer.writerow([r['date'], r['panel'], r['sub'], r['rank'], r['code'], r['name'], r['meta']])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ranks_{panel}_{latest_date}.csv'}
    )
