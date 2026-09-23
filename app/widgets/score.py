"""ABC score preview rendered with abcjs inside QtWebEngine.

本模块实现乐谱实时预览控件 ScoreView：把 ABC 乐谱文本交给内嵌的 abcjs（运行在
QtWebEngine 中）渲染成五线谱，并提供旋律试听、警告提示与按主题色替换样式。
若 QtWebEngine 不可用则退化为提示标签。乐谱更新带防抖，避免频繁重渲染。
"""
from __future__ import annotations

import json

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..paths import ASSETS
from ..theme import C

# 尝试导入 QtWebEngine；部分精简环境可能缺失该模块，此时预览退化为文本提示
try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEB_OK = True
except Exception:
    WEB_OK = False

# 承载 abcjs 的 HTML 模板。CSS 中的 %%key%% 占位符会在初始化时用主题色替换；
# JS 定义了带版本号的渲染函数，避免异步加载音色时旧乐谱覆盖新乐谱的显示。
HTML = '''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="abcjs-audio.css">
<script src="abcjs-basic-min.js"></script>
<style>
  html,body{margin:0;background:%%paper_page%%;color:%%text%%;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;}
  #bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:8px 12px;background:%%paper_bar%%;border-bottom:1px solid %%border%%;}
  #audio{flex:1;min-width:0}
  .abcjs-inline-audio{background:%%accent%%;height:30px;padding:0 6px}
  .abcjs-inline-audio .abcjs-btn{background:transparent;border:none}
  .abcjs-inline-audio .abcjs-midi-progress-background{background:rgba(255,255,255,.35)}
  .abcjs-inline-audio .abcjs-midi-progress-indicator{background:#ffffff}
  #msg{color:%%muted%%;font-size:12px;white-space:nowrap}
  #paper-wrap{padding:14px}
  #paper{background:#fffefb;color:#16181d;border:2px solid %%border%%;padding:10px 6px}
  #paper svg{color:#16181d}
  #empty{color:%%faint%%;text-align:center;padding:60px 20px;font-size:14px}
  .abcjs-highlight{fill:%%accent2%% !important;}
  #warn{color:%%amber%%;font-size:12px;padding:0 16px 12px;white-space:pre-wrap}
</style></head><body>
<div id="bar"><div id="audio"></div><div id="msg"></div></div>
<div id="paper-wrap"><div id="empty">暂无乐谱</div><div id="paper" style="display:none"></div></div>
<div id="warn"></div>
<script>
var synthControl = null, visualObj = null, scoreVersion = 0;
function Cursor(version){ this.onEvent=function(ev){
   if(version !== scoreVersion) return;
   document.querySelectorAll('.abcjs-highlight').forEach(function(e){e.classList.remove('abcjs-highlight')});
   if(ev && ev.elements){ ev.elements.forEach(function(g){ g.forEach(function(n){ n.classList.add('abcjs-highlight'); }); }); }
 }; this.onFinished=function(){ if(version === scoreVersion) document.querySelectorAll('.abcjs-highlight').forEach(function(e){e.classList.remove('abcjs-highlight')}); };
}
function createControl(version, holder){
  var control = new ABCJS.synth.SynthController(), disposed = false, pendingLoads = 0;
  var cancelled = {status:'cancelled'};
  function current(){ return !disposed && version === scoreVersion; }
  // abcjs destroy() stops audio but cannot cancel go() while soundfonts are loading.
  // Let that work settle, then release its buffers without invoking the queued play/seek.
  var go = control.go;
  control.go = function(){
    if(!current()) return Promise.reject(cancelled);
    pendingLoads++;
    var work;
    try { work = go.call(control); } catch(error) { work = Promise.reject(error); }
    return Promise.resolve(work).then(function(result){
      if(!current()) throw cancelled;
      return result;
    }).finally(function(){
      pendingLoads--;
      if(disposed && !pendingLoads) control.destroy();
    });
  };
  var ready = control.runWhenReady;
  control.runWhenReady = function(action, arg){
    if(!current()) return Promise.resolve(cancelled);
    return ready.call(control, function(value){
      return current() ? action(value) : Promise.resolve(cancelled);
    }, arg).catch(function(error){ if(current()) throw error; return cancelled; });
  };
  // Tempo changes call go() directly instead of passing through runWhenReady().
  var setWarp = control.setWarp;
  control.setWarp = function(value){
    if(!current()) return Promise.resolve(cancelled);
    return setWarp.call(control, value).catch(function(error){ if(current()) throw error; return cancelled; });
  };
  control.dispose = function(){
    disposed = true;
    control.pause();
    control.disable(true);
    if(!pendingLoads) control.destroy();
  };
  // Keep the retired controller's async DOM writes inside its detached holder.
  control.load(holder, new Cursor(version), {displayLoop:true, displayRestart:true, displayPlay:true, displayProgress:true, displayWarp:true});
  return control;
}
function renderScore(text){
  var version = ++scoreVersion;
  var paper=document.getElementById('paper'), empty=document.getElementById('empty'), warn=document.getElementById('warn'), audio=document.getElementById('audio');
  if(synthControl){ synthControl.dispose(); synthControl = null; }
  visualObj = null;
  paper.replaceChildren();
  audio.replaceChildren();
  document.getElementById('msg').textContent='';
  warn.textContent='';
  if(!text || !text.trim()){ paper.style.display='none'; empty.style.display='block'; return; }
  empty.style.display='none'; paper.style.display='block';
  try{
    var width = Math.max(420, document.body.clientWidth - 60);
    visualObj = ABCJS.renderAbc('paper', text, {responsive:'resize', add_classes:true, staffwidth: width,
        wrap:{minSpacing:1.8, maxSpacing:2.8, preferredMeasuresPerLine:4}, paddingleft:8, paddingright:8})[0];
    if(visualObj && visualObj.warnings && visualObj.warnings.length){ warn.textContent='⚠ '+visualObj.warnings.slice(0,6).join('\\n').replace(/<[^>]+>/g,''); }
    if(ABCJS.synth && ABCJS.synth.supportsAudio()){
      var holder = document.createElement('div');
      audio.appendChild(holder);
      var control = synthControl = createControl(version, holder);
      control.setTune(visualObj, false, {chordsOff:false}).then(function(){
        if(version === scoreVersion) document.getElementById('msg').textContent='可试听旋律（需联网加载音色）';
      }).catch(function(e){ if(version === scoreVersion) document.getElementById('msg').textContent='试听不可用'; });
    }
  }catch(e){ warn.textContent='乐谱渲染失败: '+e; }
}
</script></body></html>'''


class ScoreView(QWidget):
    """ABC 乐谱预览控件：内嵌 abcjs 渲染五线谱并提供试听。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        # 状态标记：待渲染文本、页面是否加载完成、当前乐谱文本
        self._pending = None
        self._ready = False
        self._text = ''

        if WEB_OK:
            # 可用 QtWebEngine：创建网页视图并配置跨域/文件访问与自动播放权限
            self.view = QWebEngineView(self)
            settings = self.view.settings()
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
            self.view.page().setBackgroundColor(QColor(C['paper_page']))
            self.view.loadFinished.connect(self._loaded)
            # 用主题色替换 HTML 模板中的 %%key%% 占位符
            html = HTML
            for key, value in C.items():
                html = html.replace(f'%%{key}%%', value)
            # 以资源目录为基准加载，让 abcjs 脚本与音色文件可被定位
            self.view.setHtml(html, QUrl.fromLocalFile(str(ASSETS) + '/'))
            box.addWidget(self.view)
        else:
            # 缺失 QtWebEngine：退化为提示标签
            self.view = QLabel('未能加载 QtWebEngine，乐谱预览不可用（ABC 文本仍可编辑）')
            self.view.setObjectName('Hint')
            box.addWidget(self.view)

        # 500ms 防抖定时器，避免编辑乐谱时每敲一个字都触发重渲染
        self._debounce = QTimer(self, singleShot=True, interval=500, timeout=self._flush)

    def _loaded(self, ok):
        """网页加载完成回调：标记就绪后立即刷新一次。"""
        self._ready = True
        self._flush()

    def set_abc(self, text, debounce=False):
        """设置要渲染的 ABC 文本；debounce 为真时走防抖定时器，否则立即刷新。"""
        self._text = text or ''
        if debounce:
            self._debounce.start()
        else:
            self._flush()

    def _flush(self):
        """在网页中执行 renderScore 渲染当前文本（仅就绪且可用时）。"""
        if WEB_OK and self._ready:
            self.view.page().runJavaScript(f'renderScore({json.dumps(self._text)});')

    def text(self):
        """返回当前渲染的 ABC 文本。"""
        return self._text
