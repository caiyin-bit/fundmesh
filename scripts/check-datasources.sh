#!/bin/sh
# 在目标 VPS 上运行，检查 FundMesh 依赖的全部数据源是否可达。
# 只用 curl，无需任何依赖：  sh check-datasources.sh
#
# 逐项检查 HTTP 状态、耗时、以及返回内容是否符合预期——有些接口会用 200 返回
# 错误页或空数据，只看状态码会误判为可用。

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
PASS=0
FAIL=0

# check <名称> <期望出现的内容> <curl 参数...>
check() {
  name=$1; expect=$2; shift 2
  out=$(curl -sS -m 20 -A "$UA" -w '\n__HTTP__%{http_code}__TIME__%{time_total}' "$@" 2>&1)
  code=$(printf '%s' "$out" | sed -n 's/.*__HTTP__\([0-9]*\)__TIME__.*/\1/p')
  secs=$(printf '%s' "$out" | sed -n 's/.*__TIME__\([0-9.]*\)$/\1/p')
  body=$(printf '%s' "$out" | sed 's/__HTTP__.*//')
  size=$(printf '%s' "$body" | wc -c | tr -d ' ')

  if [ "$code" != "200" ]; then
    printf '  ✗ %-22s HTTP %s (%ss)\n' "$name" "${code:-连接失败}" "${secs:-?}"
    FAIL=$((FAIL+1)); return
  fi
  if ! printf '%s' "$body" | grep -q "$expect"; then
    printf '  ✗ %-22s HTTP 200 但内容异常 (%s 字节, %ss)\n' "$name" "$size" "$secs"
    printf '      期望包含: %s\n' "$expect"
    printf '      实际开头: %s\n' "$(printf '%s' "$body" | head -c 100 | tr -d '\n')"
    FAIL=$((FAIL+1)); return
  fi
  printf '  ✓ %-22s %8s 字节  %ss\n' "$name" "$size" "$secs"
  PASS=$((PASS+1))
}

echo "出口 IP: $(curl -sS -m 10 https://api.ipify.org 2>/dev/null || echo '查询失败')"
echo

echo "── 天天基金系（基金数据）──"
check "基金全量列表"  "var r ="    "https://fund.eastmoney.com/js/fundcode_search.js"
check "净值历史 F10"  "LSJZList"   -H 'Referer: http://fundf10.eastmoney.com/' \
      "https://api.fund.eastmoney.com/f10/lsjz?fundCode=110022&pageIndex=1&pageSize=3"
check "批量最新净值"  "SHORTNAME"  \
      "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?Fcodes=110022,161725&pageIndex=1&pageSize=30&plat=Android&appType=ttjj&product=EFund&Version=1&deviceid=probe"
check "全量净值+分红"  "Data_netWorthTrend" -H 'Referer: http://fund.eastmoney.com/' \
      "https://fund.eastmoney.com/pingzhongdata/110022.js"
check "基金排行"      "datas"      -H 'Referer: http://fund.eastmoney.com/data/fundranking.html' \
      "https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1nzf&st=desc&pi=1&pn=5&dx=1"
check "季报重仓股"    "apidata"    -H 'Referer: http://fundf10.eastmoney.com/' \
      "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=110022&topline=10&year=2026"

echo
echo "── 新浪（行情 + 交易日历）──"
check "指数/个股实时"  "hq_str"     -H 'Referer: https://finance.sina.com.cn' \
      "https://hq.sinajs.cn/list=sh000001,sh600519"
check "场内ETF实时"    "hq_str"     -H 'Referer: https://finance.sina.com.cn' \
      "https://hq.sinajs.cn/list=sh510300"
check "交易日历"       "datelist"   "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"

echo
echo "── 腾讯（ETF 历史日线）──"
check "ETF 日线历史"   "qfqday"     \
      "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh510300,day,2026-01-01,2026-08-12,640,qfq"

echo
echo "────────────────────────────────"
printf '结果: %d 项可用, %d 项失败\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "全部可达，可以部署。注意对比耗时：明显高于本地时需考虑加大超时或做缓存。"
else
  echo "有数据源不可用。部署前需先解决，否则线上功能会缺失。"
fi
exit 0
