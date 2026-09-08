filepath = r'..\src\components\PortfolioManager.vue'

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 添加 totalCost 变量（在 totalAmount 后面）
old_vars = '''const loading = ref(false)
const portfolioList = ref<Portfolio[]>([])
const totalProfit = ref(0)
const totalAmount = ref(0)'''

new_vars = '''const loading = ref(false)
const portfolioList = ref<Portfolio[]>([])
const totalProfit = ref(0)
const totalAmount = ref(0)
const totalCost = ref(0)'''

if old_vars in content:
    content = content.replace(old_vars, new_vars)
    print('1. 已添加 totalCost 变量')
else:
    print('1. 未找到变量定义位置')

# 2. 修改 totalProfitPct 的分母从 totalAmount 改成 totalCost
old_pct = '''const totalProfitPct = computed(() => {
  if (totalAmount.value <= 0) return 0
  return (totalProfit.value / totalAmount.value) * 100
})'''

new_pct = '''// v2.9.26 修复：盈利率分母从 totalAmount(当前市值) 改为 totalCost(总成本)，与后端 summary.profit_pct 口径一致
const totalProfitPct = computed(() => {
  if (totalCost.value <= 0) return 0
  return (totalProfit.value / totalCost.value) * 100
})'''

if old_pct in content:
    content = content.replace(old_pct, new_pct)
    print('2. 已修改 totalProfitPct 分母为 totalCost')
else:
    print('2. 未找到 totalProfitPct 定义')

# 3. 在 loadPortfolio 中获取 summary.total_cost
old_load = '''    const summary = res.summary || {}
    totalProfit.value = summary.total_profit || 0
    totalAmount.value = summary.total_amount || 0
    loadError.value = false'''

new_load = '''    const summary = res.summary || {}
    totalProfit.value = summary.total_profit || 0
    totalAmount.value = summary.total_amount || 0
    totalCost.value = summary.total_cost || 0
    loadError.value = false'''

if old_load in content:
    content = content.replace(old_load, new_load)
    print('3. 已在 loadPortfolio 中获取 total_cost')
else:
    print('3. 未找到 loadPortfolio 中的 summary 赋值')

# 4. 修复 profitSummary.pct 的分母从 amount 改成成本（buy_nav * shares + fee）
old_profit_summary = '''  const pct = amount > 0 ? (profit / amount) * 100 : 0
  return { amount, shares, profit, pct, reached: holdingRecords.filter((r) => r.reached).length }'''

new_profit_summary = '''  // v2.9.26 修复：止盈汇总盈利率分母从 amount(买入金额) 改为 cost(实际成本=buy_nav*shares+fee)，与后端口径一致
  const cost = holdingRecords.reduce((s, r) => s + (r.buy_nav || 0) * (r.shares || 0) + (r.fee || 0), 0)
  const pct = cost > 0 ? (profit / cost) * 100 : 0
  return { amount, shares, profit, pct, reached: holdingRecords.filter((r) => r.reached).length }'''

if old_profit_summary in content:
    content = content.replace(old_profit_summary, new_profit_summary)
    print('4. 已修复 profitSummary.pct 分母为实际成本')
else:
    print('4. 未找到 profitSummary.pct 定义（可能格式不同）')

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print()
print('PortfolioManager.vue 修改完成')
print()
print('修改内容：')
print('  1. 添加 totalCost 变量')
print('  2. totalProfitPct 分母从 totalAmount(当前市值) 改为 totalCost(总成本)')
print('  3. loadPortfolio 中获取 summary.total_cost')
print('  4. profitSummary.pct 分母从 amount(买入金额) 改为 cost(实际成本)')
