#!/usr/bin/env bash
# 检查打包好的程序：用它分析演示音频，再打开桌面窗口确认界面能显示出来。结果写成 GitHub 的标注，
# 运行页面上直接能看到。用法（仓库根目录）：bash packaging/check_app.sh <程序路径>
set -u
app="$1"
status=0

"$app" analyze voicedetect/assets/demo/thermos-demo.ogg --framework tpl-thermos-cup --window 60 --interval 20 \
  > analyze.txt 2>&1
summary=$(grep -E "^共识别 [0-9]+ 句" analyze.txt || true)
count=$(echo "$summary" | sed -E 's/^共识别 ([0-9]+) 句.*/\1/')
if [[ -n "$summary" && "$count" -ge 30 ]]; then
  echo "::notice title=分析演示音频::$summary"
else
  echo "::error title=分析演示音频失败::$(tail -n 5 analyze.txt | tr '\n' ' ')"
  status=1
fi

"$app" selfcheck --timeout 120 > selfcheck.txt 2>&1
if grep -q '"window": true' selfcheck.txt; then
  echo "::notice title=桌面窗口::$(tr '\n' ' ' < selfcheck.txt)"
else
  echo "::error title=桌面窗口打不开::$(tail -n 5 selfcheck.txt | tr '\n' ' ')"
  status=1
fi
exit $status
