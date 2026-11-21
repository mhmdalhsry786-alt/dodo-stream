# -*- coding: utf-8 -*-
"""
نافذة التحكم المرنة والخاصة بتطبيق «دودو ستريم» على ويندوز
تتيح التشغيل المحلي، أو المشاركة المؤقتة المشفرة للأصدقاء (HTTPS Tunnel)، أو الإيقاف الكامل لحفظ موارد الجهاز.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(HERE, 'dodo_server.py')
CONFIG_PATH = os.path.join(HERE, 'config.json')
CLOUDFLARED_EXE = os.path.join(HERE, 'cloudflared.exe')
PORT = 18880

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def read_cfg():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'app_name': 'دودو ستريم', 'invite_code': '', 'privacy_mode': 'internet_only'}


def is_server_running():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/robots.txt', timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


class DodoControlApp:
    def __init__(self, root):
        self.root = root
        self.root.title('لوحة تحكم دودو ستريم • التشغيل والمشاركة الخاصة')
        self.root.geometry('590x470')
        self.root.configure(bg='#0e0a1f')
        self.server_proc = None
        self.engine_proc = None
        self.tunnel_proc = None
        self.tunnel_url = ''

        title = tk.Label(
            root,
            text='🎬 دودو ستريم — التحكم المرن والخصوصية',
            font=('Segoe UI', 14, 'bold'),
            fg='#f8fafc',
            bg='#0e0a1f',
        )
        title.pack(pady=(16, 4))

        sub = tk.Label(
            root,
            text='شغّل منصتك محليًا أو أنشئ رابط HTTPS مشفّرًا ومؤقتًا لأصدقائك على الآيفون والأندرويد، وأوقفه بضغطة زر',
            font=('Segoe UI', 9),
            fg='#94a3b8',
            bg='#0e0a1f',
        )
        sub.pack(pady=(0, 12))

        self.status_lbl = tk.Label(
            root,
            text='الحالة: جارٍ الفحص...',
            font=('Segoe UI', 10, 'bold'),
            fg='#22d3ee',
            bg='#171233',
            padx=14,
            pady=8,
        )
        self.status_lbl.pack(fill='x', padx=22, pady=6)

        # Invite Code Box
        inv_frame = tk.Frame(root, bg='#171233', padx=12, pady=10)
        inv_frame.pack(fill='x', padx=22, pady=6)
        tk.Label(inv_frame, text='🔑 رمز الدعوة الخاص بالأصدقاء:', font=('Segoe UI', 9, 'bold'), fg='#e2e8f0', bg='#171233').pack(side='right')
        self.inv_entry = tk.Entry(inv_frame, font=('Consolas', 11, 'bold'), width=18, justify='center')
        self.inv_entry.pack(side='right', padx=8)
        cfg = read_cfg()
        self.inv_entry.insert(0, cfg.get('invite_code', ''))
        tk.Button(
            inv_frame,
            text='حفظ الرمز',
            command=self.save_invite,
            bg='#7c3aed',
            fg='white',
            relief='flat',
            padx=10,
        ).pack(side='left')

        # Link Box
        link_frame = tk.Frame(root, bg='#171233', padx=12, pady=10)
        link_frame.pack(fill='x', padx=22, pady=6)
        tk.Label(link_frame, text='🔗 رابط المشاركة الجاهز للأصدقاء (آيفون / أندرويد):', font=('Segoe UI', 9), fg='#94a3b8', bg='#171233').pack(anchor='e')
        self.url_var = tk.StringVar(value=f'http://{get_lan_ip()}:{PORT}/')
        self.url_entry = tk.Entry(link_frame, textvariable=self.url_var, font=('Consolas', 10), state='readonly', justify='left')
        self.url_entry.pack(fill='x', pady=6)

        # Buttons Grid
        btns = tk.Frame(root, bg='#0e0a1f')
        btns.pack(fill='x', padx=22, pady=10)

        tk.Button(
            btns,
            text='▶ تشغيل محلي / شبكة الواي فاي',
            command=self.start_local,
            bg='#10b981',
            fg='white',
            font=('Segoe UI', 10, 'bold'),
            relief='flat',
            pady=8,
        ).pack(fill='x', pady=4)

        tk.Button(
            btns,
            text='🌐 تفعيل رابط الإنترنت الخارجي الخاص للأصدقاء (HTTPS للآيفون والأندرويد)',
            command=self.start_tunnel_thread,
            bg='#6366f1',
            fg='white',
            font=('Segoe UI', 10, 'bold'),
            relief='flat',
            pady=8,
        ).pack(fill='x', pady=4)

        row2 = tk.Frame(btns, bg='#0e0a1f')
        row2.pack(fill='x', pady=4)
        tk.Button(
            row2,
            text='📋 نسخ رسالة الدعوة والرابط',
            command=self.copy_invite_msg,
            bg='#1e293b',
            fg='#f8fafc',
            font=('Segoe UI', 9, 'bold'),
            relief='flat',
            pady=7,
        ).pack(side='right', expand=True, fill='x', padx=(4, 0))

        tk.Button(
            row2,
            text='🖥 فتح دودو ستريم في المتصفح',
            command=lambda: webbrowser.open(f'http://127.0.0.1:{PORT}/'),
            bg='#1e293b',
            fg='#f8fafc',
            font=('Segoe UI', 9, 'bold'),
            relief='flat',
            pady=7,
        ).pack(side='left', expand=True, fill='x', padx=(0, 4))

        tk.Button(
            btns,
            text='⏹ إيقاف الخادم والرابط الخارجي بالكامل (صفر استهلاك)',
            command=self.stop_all,
            bg='#e11d48',
            fg='white',
            font=('Segoe UI', 9, 'bold'),
            relief='flat',
            pady=7,
        ).pack(fill='x', pady=6)

        self.refresh_status()

    def save_invite(self):
        new_code = self.inv_entry.get().strip()
        if not new_code:
            return
        cfg = read_cfg()
        cfg['invite_code'] = new_code
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self.update_displayed_link()
        messagebox.showinfo('تم الحفظ', f'تم تحديث رمز الدعوة إلى: {new_code}\n(أعد تشغيل الخادم أو احفظه من لوحة المالك لتفعيله فورًا).')

    def update_displayed_link(self):
        code = self.inv_entry.get().strip()
        base = self.tunnel_url or f'http://{get_lan_ip()}:{PORT}'
        self.url_var.set(f'{base}/?invite={code}' if code else f'{base}/')

    def ensure_stremio_engine(self):
        try:
            with urllib.request.urlopen('http://127.0.0.1:11470/settings', timeout=1.0) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        st_dir = os.path.expandvars(r'%LOCALAPPDATA%\Programs\Stremio')
        rt_exe = os.path.join(st_dir, 'stremio-runtime.exe')
        srv_js = os.path.join(st_dir, 'server.js')
        if os.path.isfile(rt_exe) and os.path.isfile(srv_js):
            self.engine_proc = subprocess.Popen(
                [rt_exe, srv_js],
                cwd=st_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            time.sleep(1.2)

    def start_local(self):
        self.ensure_stremio_engine()
        if not is_server_running():
            py = sys.executable
            if py.lower().endswith('pythonw.exe'):
                cand = py[:-5] + '.exe'
                if os.path.isfile(cand):
                    py = cand
            self.server_proc = subprocess.Popen(
                [py, SERVER_SCRIPT],
                cwd=HERE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            time.sleep(1.5)
        self.refresh_status()

    def start_tunnel_thread(self):
        threading.Thread(target=self._run_tunnel, daemon=True).start()

    def _run_tunnel(self):
        self.root.after(0, lambda: self.status_lbl.config(text='الحالة: جارٍ تشغيل الخادم وتجهيز نفق HTTPS الآمن...'))
        self.start_local()
        if not os.path.isfile(CLOUDFLARED_EXE):
            try:
                self.root.after(0, lambda: self.status_lbl.config(text='الحالة: تنزيل أداة Cloudflare الرسمية لمرة واحدة...'))
                dl = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
                urllib.request.urlretrieve(dl, CLOUDFLARED_EXE)
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror('خطأ', f'تعذر تنزيل أداة النفق: {exc}'))
                return
        if self.tunnel_proc and self.tunnel_proc.poll() is None:
            return
        self.tunnel_proc = subprocess.Popen(
            [CLOUDFLARED_EXE, 'tunnel', '--url', f'http://127.0.0.1:{PORT}', '--no-autoupdate'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in self.tunnel_proc.stdout:
            m = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if m:
                self.tunnel_url = m.group(0)
                self.root.after(0, self.refresh_status)
                break

    def copy_invite_msg(self):
        self.update_displayed_link()
        link = self.url_var.get()
        code = self.inv_entry.get().strip()
        msg = (
            f'🎬 دعوة خاصة لتطبيق «دودو ستريم»\n'
            f'🔗 الرابط: {link}\n'
            f'🔑 رمز الدعوة: {code}\n\n'
            f'🍎 للآيفون: افتح الرابط في Safari ثم اضغط مشاركة ⬆️ واختر «إضافة إلى الشاشة الرئيسية».\n'
            f'🤖 للأندرويد: افتح الرابط في Chrome واختر «تثبيت التطبيق».'
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(msg)
        messagebox.showinfo('تم النسخ', 'تم نسخ رسالة الدعوة الكاملة جاهزة للإرسال لأصدقائك!')

    def stop_all(self):
        if self.tunnel_proc and self.tunnel_proc.poll() is None:
            self.tunnel_proc.terminate()
        self.tunnel_proc = None
        self.tunnel_url = ''
        if self.server_proc and self.server_proc.poll() is None:
            self.server_proc.terminate()
        self.server_proc = None
        if self.engine_proc and self.engine_proc.poll() is None:
            self.engine_proc.terminate()
        self.engine_proc = None
        subprocess.run(['taskkill', '/F', '/IM', 'cloudflared.exe'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        time.sleep(0.6)
        self.refresh_status()

    def refresh_status(self):
        up = is_server_running()
        if up and self.tunnel_url:
            self.status_lbl.config(text=f'🟢 يعمل محليًا + متصل بالإنترنت للأصدقاء ({self.tunnel_url})', fg='#34d399')
        elif up:
            self.status_lbl.config(text=f'🟢 يعمل محليًا وعلى الواي فاي (http://{get_lan_ip()}:{PORT})', fg='#22d3ee')
        else:
            self.status_lbl.config(text='⚪ متوقف حاليًا (لا يستهلك أي موارد من الجهاز)', fg='#94a3b8')
        self.update_displayed_link()


if __name__ == '__main__':
    rt = tk.Tk()
    app = DodoControlApp(rt)
    rt.mainloop()
