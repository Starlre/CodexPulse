from __future__ import annotations
import argparse
import copy
import ctypes
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from metrics import Session, default_home, discover, export_csv

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESOURCES = Path(getattr(sys, '_MEIPASS', BASE))
DATA = BASE / 'data'
BG, CARD, TILE, LINE = '#101725', '#182238', '#212e48', '#344563'
TEXT, MUTED, BLUE, GREEN = '#edf4ff', '#aabbd4', '#8cbbff', '#81e6c1'
KEY = '#ff00fe'


def fmt(value, suffix='s'):
    return '—' if value is None else f'{value:.1f}{suffix}'


def pct(value):
    return '—' if value is None else f'{value * 100:.1f}%'


def monitor(home, messages, stop, commands):
    sessions = {}
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / 'metrics.sqlite3')
    conn.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, metrics TEXT NOT NULL)')
    saved = {}
    next_scan = 0
    warning = ''
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_scan:
                    for path in discover(home):
                        sessions.setdefault(str(path), Session(path))
                    next_scan = time.monotonic() + 5
                while not commands.empty():
                    path = commands.get_nowait()
                    sessions.setdefault(path, Session(path))
                for session in sessions.values():
                    if session.poll() and session.turns:
                        encoded = json.dumps(session.snapshot(), ensure_ascii=False)
                        if encoded != saved.get(session.id):
                            conn.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (session.id, encoded))
                            saved[session.id] = encoded
                conn.commit()
                visible = sorted((s for s in sessions.values() if s.turns), key=lambda s: s.last_activity, reverse=True)
                snapshot = (copy.deepcopy(visible), warning)
                if messages.full():
                    try: messages.get_nowait()
                    except queue.Empty: pass
                messages.put_nowait(snapshot)
            except Exception as exc:
                warning = f'读取失败：{exc}'
                if not messages.full(): messages.put_nowait(([], warning))
            stop.wait(1)
    finally:
        conn.close()


class ZoomCanvas(tk.Canvas):
    """Scale all drawing primitives from a single logical coordinate system."""
    zoom = 1.0

    def _create(self, itemType, args, kw):
        def scaled(values):
            for value in values:
                if isinstance(value, (list, tuple)):
                    yield from scaled(value)
                else:
                    yield value * self.zoom
        options = dict(kw)
        if itemType == 'text' and 'font' in options:
            font = options['font']
            # Negative Tk font sizes are pixels, independent of Tk/system DPI.
            pixels = max(1, round(abs(font[1]) * (96 / 72 if font[1] > 0 else 1) * self.zoom))
            options['font'] = (font[0], -pixels, *font[2:])
        if 'width' in options:
            options['width'] *= self.zoom
        return super()._create(itemType, tuple(scaled(args)), options)


class Pulse:
    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()
        # Native menus/history use points; canvas text uses explicit pixel sizes.
        self.root.tk.call('tk', 'scaling', 96 / 72)
        self.root.title('Codex Pulse · 响应监测')
        self.root.configure(bg=KEY)
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        if os.name == 'nt': self.root.wm_attributes('-transparentcolor', KEY)
        self.settings = {}
        try: self.settings = json.loads((DATA / 'settings.json').read_text(encoding='utf-8'))
        except (OSError, ValueError): pass
        self.collapsed = self.settings.get('collapsed', False)
        self.zoom = 1.0
        self.zoom_percent = tk.IntVar(value=100)
        self.photo_percent = None
        self.auto = tk.BooleanVar(value=not bool(args.session))
        self.pinned = tk.BooleanVar(value=True)
        self.reduced = tk.BooleanVar(value=self.settings.get('reduced_motion', False))
        self.sessions = []
        self.selected = args.session or self.settings.get('selected', '')
        self.messages = queue.Queue(maxsize=1)
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.history = None
        self.history_signature = None
        self.status_note = ''
        self.drag = None
        self.frame = 0
        self.canvas = ZoomCanvas(self.root, width=364, height=510, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        self.base_photo = tk.PhotoImage(file=str(RESOURCES / 'assets' / 'pet.png'))
        self.photo = self.base_photo
        self.pet_item = None
        self.pet_busy = False
        self.motion_started = time.monotonic()
        self.pet_raw_frames = self.load_pet_frames()
        self.pet_frames = {}
        self.pet_image = self.photo
        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label='展开 / 收起', command=self.toggle)
        self.menu.add_command(label='会话与历史记录…', command=self.show_history)
        self.menu.add_command(label='导出当前会话 CSV…', command=self.export)
        self.menu.add_command(label='打开其他会话日志…', command=self.open_log)
        self.zoom_menu = tk.Menu(self.menu, tearoff=False)
        self.zoom_menu.add_command(label='放大  Ctrl + 滚轮向上', command=lambda: self.set_zoom(self.zoom_percent.get()+25))
        self.zoom_menu.add_command(label='缩小  Ctrl + 滚轮向下', command=lambda: self.set_zoom(self.zoom_percent.get()-25))
        self.zoom_menu.add_separator()
        for percent in (75, 100, 125, 150, 175, 200):
            self.zoom_menu.add_radiobutton(label=f'{percent}%', variable=self.zoom_percent, value=percent, command=lambda p=percent: self.set_zoom(p))
        self.zoom_menu.add_separator()
        self.zoom_menu.add_command(label='恢复原大小  Ctrl+0', command=lambda: self.set_zoom(100))
        self.menu.add_cascade(label='缩放大小', menu=self.zoom_menu)
        self.menu.add_separator()
        self.menu.add_checkbutton(label='自动跟随最新请求', variable=self.auto, command=self.draw)
        self.menu.add_checkbutton(label='始终置顶', variable=self.pinned, command=lambda: self.root.attributes('-topmost', self.pinned.get()))
        self.menu.add_checkbutton(label='减少动画', variable=self.reduced)
        self.menu.add_command(label='指标说明', command=self.explain)
        self.menu.add_separator()
        self.menu.add_command(label='退出', command=self.close)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.move)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<Button-3>', lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.root.bind('<Escape>', lambda e: self.toggle())
        self.root.bind('<Control-h>', lambda e: self.show_history())
        self.root.bind('<Control-e>', lambda e: self.export())
        self.canvas.bind('<Control-MouseWheel>', self.zoom_wheel)
        self.root.bind('<Control-equal>', lambda e: self.set_zoom(self.zoom_percent.get()+25))
        self.root.bind('<Control-plus>', lambda e: self.set_zoom(self.zoom_percent.get()+25))
        self.root.bind('<Control-minus>', lambda e: self.set_zoom(self.zoom_percent.get()-25))
        self.root.bind('<Control-0>', lambda e: self.set_zoom(100))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.set_zoom(self.settings.get('zoom_percent', 100), persist=False)
        width, height = self.dimensions()
        x = min(max(0, self.settings.get('x', self.root.winfo_screenwidth()-width-36)), self.root.winfo_screenwidth()-width)
        y = min(max(0, self.settings.get('y', 90)), self.root.winfo_screenheight()-height)
        self.root.geometry(f'+{x}+{y}')
        threading.Thread(target=monitor, args=(args.home, self.messages, self.stop, self.commands), daemon=True).start()
        self.tick()
        self.animate_pet()
        if args.smoke_test:
            self.root.after(3500, self.smoke_capture)

    def load_pet_frames(self):
        sheet = tk.PhotoImage(file=str(RESOURCES / 'assets' / 'pet-frames.png'))
        width, height = self.base_photo.width(), self.base_photo.height()
        frames = []
        for index in range(96):
            frame = tk.PhotoImage(width=width, height=height)
            frame.tk.call(frame, 'copy', sheet, '-from', index*width, 0, (index+1)*width, height)
            frames.append(frame)
        return {'idle': frames[:64], 'busy': frames[64:]}

    def pet_frame(self, now=None):
        if self.reduced.get():
            return self.photo
        mode = 'busy' if self.pet_busy else 'idle'
        frames = self.pet_frames.get(mode)
        if not frames:
            return self.photo
        elapsed = (time.monotonic() if now is None else now) - self.motion_started
        return frames[int(max(0, elapsed)*20) % len(frames)]

    def animate_pet(self):
        # Only repaint the sprite at 20 fps; statistics retain their 500ms cadence.
        image = self.pet_frame()
        if self.pet_item is not None and image is not self.pet_image:
            self.canvas.itemconfigure(self.pet_item, image=image)
        self.pet_image = image
        self.root.after(50, self.animate_pet)

    def current(self):
        if self.auto.get() and self.sessions:
            self.selected = self.sessions[0].id
        return next((s for s in self.sessions if s.id == self.selected), self.sessions[0] if self.sessions else None)

    def dimensions(self):
        width, height = (208, 226) if self.collapsed else (364, 510)
        return round(width * self.zoom), round(height * self.zoom)

    def set_zoom(self, percent, persist=True):
        try:
            percent = int(percent)
        except (ValueError, TypeError):
            percent = 100
        # Keep both the expanded panel and its controls reachable on this screen.
        screen_limit = int(min((self.root.winfo_screenwidth()-32)/364,
                               (self.root.winfo_screenheight()-48)/510)*100//25)*25
        percent = min(max(75, percent), max(75, min(200, screen_limit)))
        self.zoom_percent.set(percent)
        self.zoom = percent / 100
        self.resize()
        if persist:
            self.save_settings()
        return 'break'

    def zoom_wheel(self, event):
        if event.delta:
            self.set_zoom(self.zoom_percent.get() + (25 if event.delta > 0 else -25))
        return 'break'

    def resize(self):
        import math
        self.canvas.zoom = self.zoom
        percent = self.zoom_percent.get()
        if self.photo_percent != percent:
            divisor = math.gcd(percent, 100)
            self.photo = self.base_photo.zoom(percent//divisor).subsample(100//divisor)
            self.pet_frames = {mode: [frame.zoom(percent//divisor).subsample(100//divisor)
                                     for frame in frames]
                               for mode, frames in self.pet_raw_frames.items()}
            self.pet_image = self.pet_frame()
            self.photo_percent = percent
        width, height = self.dimensions()
        self.canvas.configure(width=width, height=height)
        x = min(max(0, self.root.winfo_x()), max(0, self.root.winfo_screenwidth()-width))
        y = min(max(0, self.root.winfo_y()), max(0, self.root.winfo_screenheight()-height))
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.draw()

    def roundrect(self, x1, y1, x2, y2, color, radius=18, outline=''):
        points = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,x2,y2-radius,x2,y2,x2-radius,y2,x1+radius,y2,x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
        self.canvas.create_polygon(points, smooth=True, splinesteps=20, fill=color, outline=outline)

    def text(self, x, y, text, size=10, fill=TEXT, anchor='nw', bold=False,
             width=0, max_width=None, max_height=None):
        """Fit the rendered glyph bounds, including font fallback, inside a slot."""
        c = self.canvas
        weight = 'bold' if bold else 'normal'
        item = c.create_text(x, y, text=text, font=('Microsoft YaHei UI', size, weight),
                             fill=fill, anchor=anchor, width=width)
        pixel_size = max(1, round(size * 96 / 72 * self.zoom))
        while (box := c.bbox(item)) and pixel_size > 1:
            ratios = [1.0]
            if max_width is not None:
                ratios.append(max_width * self.zoom / max(1, box[2] - box[0]))
            if max_height is not None:
                ratios.append(max_height * self.zoom / max(1, box[3] - box[1]))
            ratio = min(ratios)
            if ratio >= 1:
                break
            pixel_size = max(1, min(pixel_size - 1, int(pixel_size * ratio)))
            c.itemconfigure(item, font=('Microsoft YaHei UI', -pixel_size, weight))
        return item

    def draw(self):
        c = self.canvas
        c.delete('all')
        s = self.current()
        turn = s.turns[-1] if s and s.turns else None
        live = bool(turn and turn.status == 'running')
        self.pet_busy = live
        self.pet_image = self.pet_frame()
        if self.collapsed:
            c.create_oval(53,135,154,152,fill='#c5d9fb',outline='')
            self.pet_item = c.create_image(104,80,image=self.pet_image)
            self.roundrect(7,150,201,220,CARD,18,LINE)
            status = '请求进行中' if live else '最近请求'
            self.text(22,158,status,9,MUTED,max_width=160,max_height=18)
            label = fmt(turn.seconds()) if turn else '等待请求'
            self.text(22,181,label,16,BLUE,bold=True,max_width=86,max_height=29)
            self.text(188,190,fmt(turn.rate,' tok/s') if turn else '—',9,GREEN,
                      anchor='e',max_width=72,max_height=21)
            return
        self.roundrect(4,60,360,505,BG,24,LINE)
        self.roundrect(17,84,347,158,CARD,16)
        self.pet_item = c.create_image(72,77,image=self.pet_image)
        self.text(142,97,'CODEX PULSE',13,bold=True,max_width=192,max_height=27)
        self.text(142,126,'你的桌面响应监测伙伴',9,MUTED,max_width=192,max_height=21)
        self.text(326,76,'−',16,MUTED,anchor='center')
        self.text(34,174,'本次请求',11,bold=True,max_width=184,max_height=26)
        state = '进行中 · 已用时' if live else ('已中断' if turn and turn.status=='interrupted' else '已完成' if turn else '等待会话')
        self.roundrect(238,170,330,196,TILE,9)
        self.text(284,183,state,9,GREEN if live else MUTED,anchor='center',max_width=84,max_height=21)
        # Fit actual glyph bounds into separate numeric/caption slots.
        self.text(34,211,fmt(turn.seconds()) if turn else '—',28,BLUE,bold=True,
                  max_width=150,max_height=42)
        self.text(36,260,'整轮响应耗时',9,MUTED,max_width=150,max_height=20)
        self.text(327,215,fmt(turn.rate,'') if turn else '—',24,GREEN,anchor='ne',
                  bold=True,max_width=126,max_height=38)
        self.text(328,260,'tok/s · 实时平均' if live else 'tok/s · 整轮平均',9,MUTED,
                  anchor='ne',max_width=130,max_height=20)
        self.roundrect(25,290,339,328,CARD,10)
        first = fmt(turn.ttft) if turn else '—'
        count = f'{turn.output_tokens:,}' if turn and turn.output_tokens is not None else '—'
        self.text(38,300,f'首 token {first}',8,MUTED,max_width=86,max_height=21)
        self.text(180,300,f'命中 {pct(turn.cache_hit_rate) if turn else "—"}',8,BLUE,
                  anchor='center',max_width=70,max_height=21)
        self.text(325,300,f'输出 {count} tokens',8,TEXT,anchor='ne',max_width=88,max_height=21)
        summary = s.summary() if s else {}
        c.create_line(25,343,339,343,fill=LINE)
        self.text(34,355,'当前会话平均',10,bold=True,max_width=183,max_height=25)
        self.text(328,357,f"{summary.get('completed',0)} 次完成",9,MUTED,anchor='ne',
                  max_width=100,max_height=21)
        labels = [('首 token',fmt(summary.get('avg_ttft'))),('整轮耗时',fmt(summary.get('avg_duration'))),
                  ('整轮 tok/s',fmt(summary.get('avg_rate'),'')),('缓存命中',pct(summary.get('cache_hit_rate')))]
        for x, width, (label,value) in zip((34,108,184,264),(67,68,72,68),labels):
            self.text(x,385,value,13,TEXT,bold=True,max_width=width,max_height=25)
            self.text(x,417,label,8,MUTED,max_width=width,max_height=19)
        name = s.id[:8] if s else '未发现'
        self.text(34,444,('自动跟随' if self.auto.get() else '固定会话')+f' · {name}',9,MUTED,
                  max_width=198,max_height=22)
        self.roundrect(244,440,331,466,TILE,8)
        self.text(288,453,'历史 / 切换',9,BLUE,anchor='center',max_width=81,max_height=21)
        note = self.status_note or (f'含思考/工具时间 · Ctrl+滚轮缩放 {self.zoom_percent.get()}%' if s else '正在读取本地会话日志…')
        self.text(34,480,note,8,MUTED,max_width=300,max_height=20)


    def press(self, event):
        self.root.focus_set()
        self.drag = (event.x_root,event.y_root,self.root.winfo_x(),self.root.winfo_y(),event.x/self.zoom,event.y/self.zoom)

    def move(self, event):
        if self.drag:
            sx,sy,x,y,_,_ = self.drag
            self.root.geometry(f'+{max(0,x+event.x_root-sx)}+{max(0,y+event.y_root-sy)}')

    def release(self, event):
        if not self.drag: return
        sx,sy,_,_,x,y = self.drag
        self.drag = None
        if abs(event.x_root-sx)+abs(event.y_root-sy)>5: return
        if self.collapsed or (308<=x<=344 and 60<=y<=96): self.toggle()
        elif 240<=x<=336 and 433<=y<=466: self.show_history()

    def toggle(self):
        self.collapsed = not self.collapsed
        self.resize()
        self.save_settings()

    def tick(self):
        try:
            self.sessions,warning = self.messages.get_nowait()
            if warning: self.status_note = warning
            self.current()
        except queue.Empty: pass
        self.frame += 1
        self.draw()
        if self.history and self.history.winfo_exists(): self.update_history()
        self.root.after(500,self.tick)

    def save_settings(self):
        DATA.mkdir(parents=True,exist_ok=True)
        settings = {'x':self.root.winfo_x(),'y':self.root.winfo_y(),'collapsed':self.collapsed,'selected':self.selected,'reduced_motion':self.reduced.get(),'zoom_percent':self.zoom_percent.get()}
        (DATA/'settings.json').write_text(json.dumps(settings),encoding='utf-8')

    def show_history(self):
        if self.history and self.history.winfo_exists():
            self.history.lift()
            return
        win = self.history = tk.Toplevel(self.root)
        win.title('Codex Pulse · 会话与请求记录')
        win.configure(bg=BG)
        win.geometry('1060x520')
        win.minsize(900,420)
        win.attributes('-topmost',True)
        style = ttk.Style(win)
        style.theme_use('clam')
        style.configure('Pulse.Treeview',background=CARD,foreground=TEXT,fieldbackground=CARD,rowheight=32,borderwidth=0,font=('Microsoft YaHei UI',10))
        style.configure('Pulse.Treeview.Heading',background=TILE,foreground=TEXT,font=('Microsoft YaHei UI',10))
        style.map('Pulse.Treeview',background=[('selected','#365380')])
        top = tk.Frame(win,bg=BG)
        top.pack(fill='x',padx=20,pady=18)
        tk.Label(top,text='会话与请求记录',bg=BG,fg=TEXT,font=('Microsoft YaHei UI',16,'bold')).pack(side='left')
        tk.Button(top,text='导出 CSV',command=self.export,bg=TILE,fg=TEXT,relief='flat',padx=14,pady=6).pack(side='right')
        self.choice = ttk.Combobox(win,state='readonly',font=('Microsoft YaHei UI',10))
        self.choice.pack(fill='x',padx=20,pady=(0,12))
        self.choice.bind('<<ComboboxSelected>>',self.choose)
        table_frame = tk.Frame(win,bg=BG)
        table_frame.pack(fill='both',expand=True,padx=20)
        columns = ('index','time','status','first','duration','tokens','rate','cache')
        self.table = ttk.Treeview(table_frame,columns=columns,show='headings',style='Pulse.Treeview')
        for key,label,width in zip(columns,['请求','开始时间','状态','首 token / s','整轮耗时 / s','输出 tokens','整轮 tok/s','缓存命中'],[55,145,85,100,105,105,100,95]):
            self.table.heading(key,text=label)
            self.table.column(key,width=width,anchor='center')
        bar = ttk.Scrollbar(table_frame,orient='vertical',command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        self.table.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        self.history_footer = tk.Label(win,bg=BG,fg=MUTED,font=('Microsoft YaHei UI',9),anchor='w',justify='left')
        self.history_footer.pack(fill='x',padx=20,pady=15)
        self.history_signature = None
        self.update_history()

    def choose(self,event=None):
        index = self.choice.current()
        if 0<=index<len(self.choice_ids):
            self.auto.set(False)
            self.selected = self.choice_ids[index]
            self.history_signature = None
            self.draw()
            self.update_history()

    def update_history(self):
        s = self.current()
        if not s: return
        self.choice_ids = [a.id for a in self.sessions]
        values = [f"{a.id[:8]}  ·  {datetime.fromtimestamp(a.turns[-1].start):%m-%d %H:%M}  ·  {len(a.turns)} 次请求  ·  {a.cwd}" for a in self.sessions]
        self.choice['values'] = values
        if s.id in self.choice_ids: self.choice.current(self.choice_ids.index(s.id))
        signature = (s.id,[(t.id,t.status,t.output_tokens,int(t.seconds()),t.ttft,t.input_tokens,t.cached_input_tokens) for t in s.turns])
        if signature == self.history_signature: return
        self.history_signature = signature
        self.table.delete(*self.table.get_children())
        status = {'running':'进行中','complete':'已完成','interrupted':'已中断','incomplete':'未完成'}
        for i,t in reversed(list(enumerate(s.turns,1))):
            self.table.insert('', 'end',values=(i,datetime.fromtimestamp(t.start).strftime('%m-%d %H:%M:%S'),status.get(t.status,t.status),fmt(t.ttft,''),fmt(t.seconds(),''),t.output_tokens if t.output_tokens is not None else '—',fmt(t.rate,''),pct(t.cache_hit_rate)))
        summary = s.summary()
        self.history_footer.configure(text=f"平均首 token {fmt(summary['avg_ttft'])}   ·   平均整轮耗时 {fmt(summary['avg_duration'])}   ·   加权整轮速度 {fmt(summary['avg_rate'],' tok/s')}   ·   缓存命中率 {pct(summary['cache_hit_rate'])}\n缓存命中率 = cached input ÷ input tokens，按输入 token 数加权；未知数据显示 —。纯流式生成速度：本地日志不提供。")

    def export(self):
        s = self.current()
        if not s:
            messagebox.showinfo('暂无数据','尚未发现用户请求。',parent=self.root)
            return
        dest = filedialog.asksaveasfilename(parent=self.history if self.history and self.history.winfo_exists() else self.root,title='导出请求指标',initialdir=str(BASE),initialfile=f'请求记录-{s.id[:8]}.csv',defaultextension='.csv',filetypes=[('CSV 表格','*.csv')])
        if dest:
            try:
                export_csv(s,dest)
                self.status_note = 'CSV 已导出'
            except OSError as exc: messagebox.showerror('导出失败',str(exc),parent=self.root)

    def open_log(self):
        name = filedialog.askopenfilename(parent=self.root,title='选择 Codex 会话日志',initialdir=str(Path(self.args.home)/'sessions'),filetypes=[('Codex JSONL 日志','*.jsonl')])
        if name:
            self.commands.put(name)
            self.auto.set(False)
            self.selected = Path(name).stem[-36:]

    def explain(self):
        messagebox.showinfo('指标口径',
            '首 token：采用日志中的 time_to_first_token_ms；通常在整轮完成后才可获得。\n\n'
            '整轮耗时：从 Codex 开始处理请求，到整轮完成，包含思考、工具执行和等待。\n\n'
            '输出 token：日志报告的 output_tokens，可能包含推理及工具调用 token。推理 token 是其中的子集，不会重复相加。\n\n'
            '进行中实时平均 tok/s = 日志中当前输出 token 数 ÷ 当前已用时间，每 500ms 更新显示，收到新的用量日志时更新 token 数。完成后显示最终整轮平均。它不是纯流式生成速度；本地日志缺少逐 token 到达时间。\n\n'
            '会话平均耗时为算术平均；平均 tok/s 为完成请求的总输出 token ÷ 总耗时。中断和未完成请求不计入平均。\n\n'
            '自动跟随按最近用户请求活动选择会话，无法获知 Codex 当前前台标签页。右键可固定会话。\n\n'
            f'数据源：{self.args.home}\n指标保存在：{DATA}\n仅读取日志，不发送网络请求。',parent=self.root)

    def smoke_capture(self):
        from PIL import ImageGrab
        from types import SimpleNamespace
        self.smoke_original = (self.collapsed, self.zoom_percent.get())
        self.collapsed = False
        results = []
        was_reduced = self.reduced.get()
        self.reduced.set(False)
        self.pet_busy = False
        idle_a = self.pet_frame(self.motion_started)
        idle_b = self.pet_frame(self.motion_started+0.6)
        assert idle_a.get(50,4) != idle_b.get(50,4) or str(idle_a) != str(idle_b)
        assert len(self.pet_frames['idle']) == 64 and len(self.pet_frames['busy']) == 32
        self.pet_busy = True
        assert self.pet_frame(self.motion_started) is self.pet_frames['busy'][0]
        self.reduced.set(True)
        assert self.pet_frame(self.motion_started) is self.photo
        self.reduced.set(was_reduced)
        results.append('Animation: idle loop, busy loop and reduced-motion mode OK')
        for percent in (75, 100, 125, 150, 175, 200):
            self.set_zoom(percent)
            self.root.update()
            width, height = self.dimensions()
            assert (self.root.winfo_width(), self.root.winfo_height()) == (width, height)
            assert self.photo.width() == round(self.base_photo.width() * self.zoom)
            # Click targets must follow the same scale as the drawing.
            ex, ey = round(326*self.zoom), round(78*self.zoom)
            event = SimpleNamespace(x=ex,y=ey,x_root=ex+self.root.winfo_x(),y_root=ey+self.root.winfo_y())
            self.press(event)
            self.release(event)
            assert self.collapsed
            self.toggle()
            results.append(f'{self.zoom_percent.get()}%: geometry, pet and click target OK')
        self.set_zoom(75)
        self.canvas.event_generate('<Control-MouseWheel>', delta=120, x=30, y=30)
        self.root.update()
        assert self.zoom_percent.get() == 100
        results.append('Ctrl+wheel: 75% -> 100% OK')
        self.set_zoom(125)
        self.root.update()
        x,y = self.root.winfo_rootx(),self.root.winfo_rooty()
        width,height = self.dimensions()
        ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(BASE/'preview.png')
        self.collapsed = True
        self.set_zoom(75)
        self.root.update()
        x,y = self.root.winfo_rootx(),self.root.winfo_rooty()
        width,height = self.dimensions()
        ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(BASE/'compact-preview.png')
        self.collapsed = False
        self.set_zoom(125)
        assert json.loads((DATA/'settings.json').read_text(encoding='utf-8'))['zoom_percent'] == 125
        results.append('Zoom persistence: 125% OK')
        (BASE/'zoom-verification.txt').write_text('\n'.join(results),encoding='utf-8')
        self.show_history()
        self.root.update()
        self.root.after(800,self.smoke_history)

    def smoke_history(self):
        from PIL import ImageGrab
        win = self.history
        x,y = win.winfo_rootx(),win.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+win.winfo_width(),y+win.winfo_height())).save(BASE/'history-preview.png')
        self.collapsed, percent = self.smoke_original
        self.set_zoom(percent)
        self.close()

    def close(self):
        self.stop.set()
        self.save_settings()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home',type=Path,default=default_home())
    parser.add_argument('--session',default='')
    parser.add_argument('--smoke-test',action='store_true')
    args = parser.parse_args()
    if os.name == 'nt':
        try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception: pass
        # One instance per installation, allowing independent copies if desired.
        import hashlib
        name = 'Local\\CodexPulse_' + hashlib.sha256(str(BASE).encode()).hexdigest()[:16]
        if args.smoke_test:
            name += '_smoke'
        handle = ctypes.windll.kernel32.CreateMutexW(None,False,name)
        if ctypes.windll.kernel32.GetLastError() == 183: return
    Pulse(args).root.mainloop()


if __name__ == '__main__':
    try: main()
    except Exception:
        DATA.mkdir(parents=True,exist_ok=True)
        (DATA/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
