const percent = value => value == null ? null : +(value * 100).toFixed(3);
const comparison = echarts.init(document.getElementById('metric-comparison'), null, {renderer:'svg'});
comparison.setOption({animation:false, legend:{data:['吻合度','第一选点率']},
  tooltip:{trigger:'axis', valueFormatter:v => v == null ? '暂无数据' : v.toFixed(1)+'%'},
  grid:{left:48,right:28,top:40,bottom:32},
  xAxis:{type:'category',data:['黑方','白方']}, yAxis:{type:'value',min:0,max:100},
  series:[['weighted','吻合度','#286773'],['first_choice','第一选点率','#c1813c']].map(([key,name,color]) => ({
    name,type:'bar',barMaxWidth:55,itemStyle:{color},
    label:{show:true,position:'top',formatter:p => p.value == null ? '' : p.value.toFixed(1)+'%'},
    data:['B','W'].map(c => percent(metricData[key][c].rate))
  }))});
