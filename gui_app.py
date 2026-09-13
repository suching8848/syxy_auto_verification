"""图形界面入口 — 让校园网自动认证工具变得看得懂。

这个文件只负责「界面」和「两种启动方式」，认证与检测逻辑一行都不复制，
全部复用 auto_login.py。

场景 A 与场景 B 的行为其实是同一件事
------------------------------------
两者都是「启动后一直探测网络，断了就立刻自动重连」，区别只在启动方式和外壳：

    场景 A（本文件，双击 exe 的默认行为）
        你手动点「开始守护」→ 持续探测 → 断网自动重连 → 一直跑到你点停止
        有窗口、有托盘图标，关窗口缩到托盘继续守护

    场景 B（`--silent`，由 auto_login.run_silent_mode 处理）
        每天到点由计划任务静默拉起 → 同样的探测与重连 → 跑满时长后自己退出
        无窗口、无托盘图标、无任何提示，只在 logs/ 里留一份日志

线程模型（最容易出 bug 的地方，务必遵守）
----------------------------------------
    主线程        Win32 托盘消息循环（复用 core.TrayApp 的隐藏窗口）
    窗口线程      tkinter mainloop，root.after() 轮询队列
    探测线程      core.run_detection_loop(stop_event, status_callback)
    手动线程      用户点「立即认证」时的一次性 core.do_auth

铁律：任何非窗口线程都只能往 queue 里放数据，绝不直接调用 tkinter 方法。
tkinter 跨线程调控件会随机崩溃或卡死，所有界面更新都必须回到窗口线程执行。
"""

import argparse
import base64
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import auto_login as core

# ── 界面状态 ─────────────────────────────────────────────────────────────────
STATE_IDLE = "idle"            # 待命：还没开始守护
STATE_PENDING = "pending"      # 正在检查：刚启动，第一次探测还没结果
STATE_RUNNING = "running"      # 守护中：网络通着，随时准备重连
STATE_AUTHING = "authing"      # 正在认证：探测到断网，正在重连
STATE_FAILED = "failed"        # 重连没成功，仍在后台重试
STATE_EXITING = "exiting"

STATE_TEXT = {
    STATE_IDLE: "待命中",
    STATE_PENDING: "正在检查网络",
    STATE_RUNNING: "守护中",
    STATE_AUTHING: "正在重连",
    STATE_FAILED: "重连未成功",
    STATE_EXITING: "正在退出",
}

STATE_COLOR = {
    STATE_IDLE: "#8a8a8a",        # 灰
    STATE_PENDING: "#d98a00",     # 黄
    STATE_RUNNING: "#1a9c4a",     # 绿
    STATE_AUTHING: "#d98a00",     # 黄
    STATE_FAILED: "#cc2b2b",      # 红
    STATE_EXITING: "#8a8a8a",
}

MAX_HUMAN_LINES = 400
MAX_RAW_LINES = 2000
POLL_MS = 120
PROJECT_URL = "https://github.com/suching8848/syxy_auto_verification"

# Window/taskbar icon (64x64 PNG, base64). Embedded rather than shipped as a
# file so the --onefile exe stays self-contained. The tray icon is a real .ico
# loaded via LoadImageW; tkinter needs image data, hence this constant.
WINDOW_ICON_B64 = (
    "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"
)

# ── 托盘菜单命令（不使用 auto_login 里面向控制台的那几个命令）────────────────
CMD_OPEN_WINDOW = 1001
CMD_MINIMIZE = 1002
CMD_EXIT_APP = 1003

# 每天自动守护用的计划任务名（与 setup_task.ps1 里的 -Silent 分支一致）
SILENT_TASK_NAME = "CampusNetAutoLogin_Silent"

# Tray icon (.ico, base64). Embedded so a lone CampusNet.exe still shows the
# right icon; assets/campusnet.ico is preferred when it is present.
TRAY_ICON_B64 = (
    "AAABAAcAEBAAAAAAIAAwAwAAdgAAABgYAAAAACAAcAYAAKYDAAAgIAAAAAAgAJYKAAAWCgAAMDAA"
    "AAAAIAChFAAArBQAAEBAAAAAACAA4yAAAE0pAACAgAAAAAAgANRgAAAwSgAAAAAAAAAAIAAWFwEA"
    "BKsAAIlQTkcNChoKAAAADUlIRFIAAAAQAAAAEAgCAAAAkJFoNgAAAvdJREFUeJwV0EtPU1kAAOBz"
    "zn3f1nLLLS1FaA1atIA4QZz4wJnAgDrRhQmaqCs3rnXlwj9gYuLChYlLF250YxxN1KAj6jjqMIBl"
    "uMrDmWkpVyy0pQ/u6/Sec4y/4Es+eO3GC0EQZjMfFmtrTamkGtYYA9hxGaOSIiOOo5RyAl/Or3Kc"
    "oGyREcaNvyfff0ZW++BAMKJDCAnGTt2CAAmy7GMfQQgo4xV5o1AUVRXdvX0zK+COH3b7Lqa+zxij"
    "HIok4p5rmYaRn190HA8AEGrWKANOzeK1gT1b+/tKOTMU1QEEeNMOrnydNYxmnie2a1dqZddT9u9F"
    "EISjzYQQdOyXw70BmVG6WbdEScSOl5l4lVBk2yM4rPeMjUZFgC0bQCgqko8x6lDlrOUAwMjyas12"
    "G44zOjpc9ojcvX3w8pneCycC6STEGEDEiTxpEPT7l2KVUJHQ0bZY8EsxHdhydPBAONU+dOnU8f40"
    "ly+mROG77Lp2pc7zHF/BDcgai6/fLJWK58ZOjfw00NsXV1vh4z9nSzsqQ8gvxPWXn4rCpostNxxv"
    "QT72558+56pVTw7dN+bEKBdS0aGuxNWzv55M6gf7U5nltdy7KVKt8BwItDSj75XrRTEQSh8bHL5y"
    "fmql4JXrpeIGkHio628mF98/e6uVykuPnni1aq2wziuhYGtXlw3p8YunpUIlHdHEeEu0o71qmtdv"
    "PajbriQHZdUnZd+dn8sZs3wDwG3J2MnD6anXMyOx8KGhHwFgj+6P352YdDEbO9gT0XXcADPTH7L5"
    "nKyKPLWsDZ92QXJkVwwIwh8Tf925N/5PZiGyrw/ujD8cn+rpTgebxOGRn5dzK9MzGd4xV4Od2y78"
    "Np0Mq/n/TQsK3tJ/3akOsVIL7Tqw/DGPGCOEYs9OdiYYo6hufl3/Nys2aStMEranEnt68vkshYLW"
    "pAPcAIk2Y+GzLIkQIUronGEgrUWPdib1ttawrsWSW13LUWQFcUCVJVyttXWnspZd36gCAEzTXFtb"
    "/wb53X2B20MUyQAAAABJRU5ErkJggolQTkcNChoKAAAADUlIRFIAAAAYAAAAGAgCAAAAbxWqrwAA"
    "BjdJREFUeJwllGtoW+cZx9/L0bnIOrrZutqxLDu+51bHmnNpY+IlS2majLKsKXQZYyUwGBuDjW2f"
    "ujHYOroP/TBG2UYZDAqDXaBLWdISN62T2GnqJLZi2fH9Ism2LEuWdHR0bu/7niHn+fR8eHj+PP//"
    "ww/+/r1RxpiDd9jUvvvF56vlnUBfhzcawZIIAMActm0bAGjbts0YqLfgeWEOb84tOnhnpKd9a3aW"
    "A7YtSpKqKDdufswdjPaPvAwQZITajEEI93K7elWjlHoDfqdHhhAwyp4vooTwbnclu+NVAo3tcY5B"
    "uJvLfXrnU/dQX7SrW1OqAAAI67qUEEaop8kPgM3xDghAYTPvDfggxjZjGGOEsWERtVDytYThwMDV"
    "jdxO4tpVfzymKVWE0XNBCCGqjyIAAMfzRq1GCakWFUaZP9JECAEQ8LwwOzEtCXx8oJdzaEr/lVcb"
    "29tKuR3BKe17YEMIiWntbWUESUIIzE98qW1tY8agKNjepsTFb3A8ZpTZNgscCJWyeQAAsg71Bbu7"
    "NVWt7JaIbkJU9xVzuFQoJhhEM7P3/vSXw2LD+YHjiZ7+Tk9TtKZtTT1FmKvfTqnskQWJp5Ry588N"
    "O0XB5XONFktqteb0yoZFbAA4G341+Si9mf7O61cW0+up1IJp0cZ49GBbWK1pNaUqOSVKqUMUGGWW"
    "bnCHAv6aRR4XKy6/d+bepFN2SS6pnjUhD+fmr156eWL6aa5mRQ40H371VOfIwMqjJBtLWozZdQ8A"
    "xAgLnKkbiIMgpxmZmlFVa3G3XHq2rKgaxNjcK73/7m+8Hnl7Tzk6fGL4J1eiL/WrNbWpPSa6XcAy"
    "n7uJMNoP2EZ3twsL5arLwWm60eOVfzhyhiynZ8cfHw2FOuJthmZ1DQ98/WevN7cFh/zeoy3h9adL"
    "hwMeaoP9DwF6VaWUYg6hLc2sUYZ5B9D0qenkZ3c+O3+w7a3BgVO9XapWvfbmeReyUzPzPVJDHAsL"
    "D+bO8ujQgVDFpILAb69tlnJFZAPeKXGIMUaZXlHmPr5plyvJmdRlhK++dtnjc/T3hRv87rep8c/7"
    "U08K1VUHOhd0nxw8/vaHd2SvZ21mQZLdkBDACw5R4EzTUivq8uhoAHMsEjYRd2PsAW6U//y7HwBK"
    "gG4MnT0+NJIANQ0UcsBm2Uz+/tL26tJ63+lTELG15FzP2Rct00SSq6GYztiFPZfPrXJ8g7/p4i/e"
    "KnY335tM2RAl//VJaSVDNZ1QSrAAhIbGg+2ufA6uZXdnU8/+/d9gvFlq9ECIEC+KZkXxiKKFkNTg"
    "OvLGSPeFhOyVy1UdQjt6rNcTCUBCOEmwZXlFMb57/VepqYXBgWNzt24j3dpZWtlMptRiiaOEBDti"
    "0DAqqnb8zXNnLp5M+HybpYcDiX4AUVNnjBoGdnvGbt//4MNPDI1sZCrt8dZyZdcZDXECgNvZjdUV"
    "hjnOMnR/PKbvlSISD2Ihp0lu3354pqMl0hK0KlWHyGNR2Mpk3//rf7r7j37z0tDYF5P5XWN9I+3e"
    "yBJiNYXDCCHLMBCHuUqx3Oxg7/3oUiS5+OAft7r0yrd6m4FFHD6/qps//eUfX/n2z8HJY6Pj49NP"
    "krWaAhE8cTrxvWtvdHf2ZFay+e08RBhe/vXfoCxb4+Mf/fb7XDQIDBNwGDhERSMf3bz7zrsfdHR0"
    "1GTJ/+JgaTXjXt6KtUYbg0GAAEZYEsWd7fzY3fHs5iY8ff2deOJYOrUgp1deGepu9smFivp0a288"
    "ucITtL66dvHSaWzDZ4L40vXXbv34D1cunDNtWmcuAHVGOxyiIE49muJYvqDXtGC8Zc8r/32jCJb3"
    "yjtFo6p1nviace9Bo9+t66hWKMKQjHmehgJPHk8PnkqoVRXtl2VZEIJypYRKmc3ss6W16VRxaS0Q"
    "CvjDYY/P98KFYQ6AZCq1mM44na7unl5mUmqYrYMvJNfTalnBXJ1H+yBFlNL5+UUUajvgEHiX7I0d"
    "6VMrilqpBjpigstlGqZtQ5/LaRFL9sjAMCglnoDX2dU5+WhKFATbroNUksSdXL5YLHHIsPytEVFq"
    "IJYVamsFqA5Zyii1iNPpdHsbFKXCS4JdM6hhcg4U6e9ZvvG/I/mC4HKWS2VBFCYmvmwKN/4fW0sr"
    "BmC+DX4AAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIAgAAAPwY7aMAAApdSURB"
    "VHicZVZpbFzVFb73vvvem3nvzT5eZmzPeCexHYc4IYnrhLKIEEggKQkFWspaCgX+VBS1VKqgrYpa"
    "tYAq1B8EqYhVLaCCgNJAiLLY2Uy8ZHFiO8FxvM6MZ9/edpdqnBahcn7ep3Pf+c4933c++IeX9oPl"
    "YIwpipLP5/bt/exyaqFmZWuwKer0uZEkVz5zDhECACCEOOeMMfDtgBAyOnFksKa5NdgQYghM9R/F"
    "y7mVFEXVJs+Pf7b/c/+q1g233SU4ZEYpowxwXrkXY2LZlm5QykQJy05H5fT/gnMKYPTq7qmhs26/"
    "V1Dl+tVdmHOOEFJU1/CXg58e+nzdXTv8DfV6sWzrJoSVojjnAsapxURqIeFwOCijoijWtTUCwJFQ"
    "QcMpB3AZAEKkVI7NzbuDgcTsfF1nq+B0YkmSisXi+NjZL44e6Hv4bllzl3J5JAgQwa9xc8YkSWxe"
    "2SapjgpYWoEmIDQ3Oe2vqVI8KiMUVNpDFbfmDQQSM3FR15lNAeD47bdfm526iFS17+F7JcVl6QYS"
    "hG+j91T5iU0s00QYiw5ZRIIgoGC4dv7i5ebONrBcDYTQtuxgqCaTyGVn5qhJkEPAdaQ0JYLrHrxH"
    "9nos04SCcKXpXwdCyDRNqlPN7RYwNgqFmXPn0zOztmVVR+oE5FyKJWsjtbZtw0pPAWW0OlqbmJox"
    "y4aquPB8Ntvz/V1qVdA2zXwyLYqiFvAxQq50H4tiNr5kLCQAYxmMcgvz40eOOyGq8vsxFiZOn8vo"
    "RrhnbXV4+5XbIYSUUZdbc/vcAFUqxeWW5paW1kK+6HA6LMOiBvFUBSi48mwAIVQs6z2Md6/ufOo3"
    "z3PLvv/uOxsiDUvJdC6XgxBxzs5NTF46OdK2ab1Z1iujzAHCguZzWWVDC3px303XNbqdaQybva7P"
    "c7nY5VjVN1rEAWe6mSuVnn/hxUDQ89jDjxw7cWzfwDGLI0KZIAihau+WTRsOnDmnF0tYFPmVXA4U"
    "l1ZIZwPRMF4VrmlUlfOAGZatej1wNpaai1dFQpZhgGXUGMH3Dh6Wqf30o4+99tbbSZuEos3MIuHO"
    "ls5tvSaw+l95t6OzOVbSvQGZ2gQCyBh1uNV8OsMZxYqACOeXCvpCSXcpikDIheNDbv+NkiJbpskZ"
    "J2WjvSG8YW3PyaGTSQoizSsIsa++49q263sgApLi0HdtNb8cg5Qul16pinGOJcwIZZQjjJDFWMG2"
    "ZQHbxOaE7N68cfzIoF4yNLerWCgKZf23v/r5zTd8d3pu3uv2eeuDW37xg6u2rtdts1TWbdOkjHlV"
    "hRH7v3xbfupl+nAIIB5NZiEEBuW4MgDcyuV71gSqrt30xv5DwZWt8Zn5be0tsoiJTW3CW2/s7r1v"
    "m4UYK5UbXRoBPG2R/PRib33V+ctZBAFZ1gXAGbEsjgBCEM+WTc65iBCAgFjEJUqFTLq1vfVnO2/5"
    "7MRwZ2Pk2jVd6WyhtjbY1R4FrWF3OCDmCju7mvwAxGz7Jy+8ubvOX60q+lQGcC7JUjqeLGZyqlOR"
    "ZBkKCGMIK0Jm25KqZOamJr48+afR4XKhsP3WW564a3exkC/pZm2tp6830t7yxPYHnl2Yjfddt3aw"
    "aI/PJy58Nbez2v3oLdc898YXDodDlKR0IjU/OdO2umNh4qI/XMMYwwDBfCqjedzlpdTgO+/KlCPJ"
    "HfAHsoXi/GJMU53X9ESam4OAkrqG0Id7fv3up/2Fidmzl+PtAe2h6zprAhotG8NzyWDrVYnZ+bmv"
    "5trWdDJCzHzR29NFLBsjhDJLaVGUB15/s1pRm1pahk+ddlXXHDw5PHR+4u9/e765I8QKZYQxEGC0"
    "veHplQ8CwIBeBKklYBJG2TuHRi8VbGX4jGmRro3roMAH3/viqr71FYGivEI8h0udHh6xEqkVK1eM"
    "nBsLr+p2V9e09vSsuGfbnv39dsmAAjbzxbmjI3a+RAp5WiwSw7ZKlmXZnIGutau11NKp99/XREiI"
    "se/Flz1exRsJWaZRkXAAgKoo0yNn2tvbUqmkO1zPKVMDnr6f7rj+vq1zFrt4cRYq6sTegeTENJIl"
    "jJAAIMRYrK6WQiEh2jQ2ciY1Pr7j9t1Th4+Nvv5O1BvQwjWyqogOGWEBVzalqpiFXLC1MV0uAo4C"
    "0VDvo7cLipxP50UIFKcDEKNty3ecQR8gFDAKEBIUjRSKyaK+56W/PPu7V9eu62HEAKaRi5WgL5A8"
    "MWTrhtPj0YJBzCh1alqgoV5EGAHBFXD3PnKb5nJcH6qdWUwuIBhtCjPDcvo8wLQY54wDxuirL791"
    "aOB0LldKp8trru52iEJ9yB9saUjMx7FoS5QnjgyYNuWChCGElmWu+d52dTGRHJu85v5bXWHvLaFQ"
    "kyS/8tbeR27dfGWZAEIZ50gSkayMnRz9x/uHtmy9qT7sM/TC0NBFj9u1d+++uK47BBSuD9ucccYh"
    "4MS2MYcVYluWXZocf/KBbXtOX2SK48OJ2cnRqdtWNmzqbmG6ATignAqaMn1hZnJqfs/rH/jqa7xu"
    "cd3aqF7Sz5y+EIlGHn38x+Nnxw8cHDh3dqIuUqdqim0TDiEGlIqKIzUXv7mj8d5dvdong4dGzjMZ"
    "P7XuqnVt9VYyIzgdglMGouPQ/iPP/fH10cFTW559XHY6nv/9XzOZ3fWhKsZp2TAYoK0dbU1tLV+e"
    "OHn8+IklDOsiIQlL2MjmnC5NcWvT85cAgztvWLNzc9eyJFJgU6neB5A0eW7ixZff/uDjw3fu2p6l"
    "RPZ5EpfjbdtvHBtbmJnJOB0KFgWbMF3XIYJ9125c1d15+PDAqTNnfX4fSs0t2rZd39p4MlV65pcv"
    "x+aXAGWVHxCWJHTvv/offuiZux949uDA2cTCYnNz1ear2/tf+6hnS+/6H24dm7zQ2dHZ0NhAKIEQ"
    "IoQggMVSSXJKt+3Y9uD9P3JKMi6ns4xUpqPnjm0fHzzy0ZN/bvS7XLKQLZnjl2ahv4Zr6tpoCxa4"
    "CPn0dKyuJqCVCqV8pqopQqv8w0MjPRvX2aVSRUT/t2UppblCvi4S2tTXizXdzMTiNY2RTCzesXkD"
    "FzdlFmMly1ZUDRn9a7dvMcrFuU/2GaYBhYpDGz09VZkoymzDaty4fvCfH3etXvX17V/vA4xxoVA8"
    "eOggysRi08OnGaELF78a/aK/HE/5/YGqqmDyq+nq5kbJ49JzxalL04vFQiKd83jc6zes93m8pWxB"
    "wKi6qYFXV584Nqio6jfdasUMIlQqFHL5AvLWVntDNSP/PgBs0LamOzG/cGHo1MTgqKSp0dUdjFII"
    "QKFc4oYlI0QZczgdDhGXszlREqltrbh+8/D58Wwqg0VcWZhXnC7joiguLMbSyQyWAfJFw43dXYBx"
    "wSm5a4KUEIgFJAi2bqKKreAidkYjoUwmZ5kWA1zTFD1XxLIoOgXV7/Ot6jjSf3TbjluLdhELFeZe"
    "8RanTp0J14cQBrCUycqqE2BALYvYNgeA2YQYJgAVFhLTgpDLTpkyoutlCKFL07LTCxAgp0sxdb1p"
    "/bqJmbmlxbgsy/lcPhFPFIvFgf6BeDwRjoT+A7BShcPpFN7AAAAAAElFTkSuQmCCiVBORw0KGgoA"
    "AAANSUhEUgAAADAAAAAwCAIAAADYYG7QAAAUaElEQVR4nI1ZZ3Rd1ZU+59xz66t676n3bhWr2nKR"
    "KzY1ENsQIKasgAlJaAmGsDKZzGTWJGuySDLJrIRJJhkCQxJIgCQMhhhjm2YjgyRXybIs2Zas3l7v"
    "t51zZt0n2xhMwhz9k2757re/vfe3t+CT//E2+PihlHIcJ8vy6Oj5vXt2T02PZRXnZ1eUe0uLZI+T"
    "l2xAwICyj25gjJgmh3kAGIAQfNZhjPGiMHXsxMTQSNuNNyg2xTB0xgFI2Qe/+S2+Eo0oirph7Prf"
    "P/f2HsldUtW54w5HQS4nioBSQimj7BIaxhiEEHKIx9KF31DK2GVYP+1ACA1NL+1oNyB/bN+Bjms2"
    "cLJgEEOUpLKV7R8DRCmVZXl2du7F55+PUa3z3ltzait0wyS6Yaia9azMz6WDMQaA6SlN13VCKIeg"
    "IEuYx5+ByHoOCM37a1qWRuaC46eGa1a1mtQ0dL2wrv4CIMYYpVRRlPHx8d/89y9y6qpuuH0r4/lk"
    "PGl9UeZ84qEIoXAgOD0yQVQDIQ5AZuq6zeWsbmlggC4y8alsMcYwxvNjkwsT01Wtjae7elOxaqyI"
    "lFCGOUwpRQhhzPMCPzMz/dwz/1W6sqll6w162qBpFSL0t76SEDI3OunJ8nrzszmBhwgQw7TiCRe1"
    "BHVNw7yA0CdhQQgppeV1Nf0fHnO6fTavOzA9U1RfrRMNAIjtdrumafPzM5Sy3z3zdH5zffu2z6cS"
    "KQbA30GzeKqb60VJMhlh1HonxsIlGfE8nhieVhNa3fIGk5BP3Egp5SWpqnHJ2Jlxm6xEZwJFtdWL"
    "asB/feO1keHTgVDATKZLlrW2bbsxlUwvfsdn6ABCyGPN0C8Jy+L84o2GYRaWFg8ePTkxNFq2pEoz"
    "9csfaOnaMNw+r3M+EpqaRUQ3NQNgCBhDQ70HiZnU47H8pvr227aYhmkx/P/IXsRxF2NhCRAAwEuS"
    "ZLMJoshhDnHI7nM1drT4Z/0Rf9hS+hV6ooz6inMpB9OqYaR1hJAlrydu+Nz3dr/mrS5b9sVtmmFQ"
    "RiFCKBPmvwklE8pkLGZ3OhDHEco4jImu+0dG/WfPJRYWtFgS8ZzscuRV12R5XRPnJ+qy6hGElyOC"
    "EJrEtLsUT55vciGsJpNSlkIAwAdOHE9BtPFLd1OIqGkgzOmqFpiaya8ozaTnx471BQIfnJiJzi4k"
    "k4l0Wi2pqXK6HZGp6XOHuhfGxomaAlaK8Zl79TPv97iLi7x19WqyUnEqJjGvUAL0ZXtn8AiW+EUK"
    "8O6h05333cvbFDWZQhY3yNSNyGywsLSU4U/JWw5x8XjCltZ2XHdt39nR93t6j548mYxGGVUrqhta"
    "25qLi4pkSdLSCX842nPk+PlzI5GD70NCl2+57hMfaKUbIbLLIdgEDiLrjxDg/IaGrKqKeCLJc5xB"
    "KQeAwAuUQ7puCFhi4IpCwhjEnCMSzQks1OS4Xx85mwiHS8pK77rj9ualDfN+/+lTw2fPjKRVNdfn"
    "u33bVkkRT/SdPNjVPT9Sm1tTqWva5SRZpVhR7HZ7YGauNGeJaZq4al1nqcNGTXMqqTZ4XAJCA7G4"
    "wPPh+UBedQnTyRUks3ggVGqTj46M/vTPf1KjkVWdGx5/5IGTg6f+5Xvfn5gJUQhFmx3zfPxwH/fX"
    "vZXlRd/c+ZDbaX91/7tFS6r0KxQJIVBczqg/tNiRcEVl2QqPA0PU4w+2eV1xXe+PxXLLiibPjeWW"
    "FSKILifJItkkJKEGBNyz/101Gt+46ZpHHvzaL3/xq7feeTu/dkl5c5MgSxAhQ9MceV5fVeH4wOBj"
    "3/7Ow/ff6cnzJWNJJPKfkAGh1OHLCs0tEJNYIStUJMSgyRhC4FQkljSJqRNvrm92dHzs5HBNe1NK"
    "SyF4oUKyTDtVHLaed95Tg4GyipoHduz49x//tLuvb+lVmwXJThhRkyks8LWbV9ZuapOc0ib3tmd3"
    "/vD85PSadcvPBkIFpQWGblxiHQJImCnbFUqIoWtIEJBT4ClgCIK0yT6YDw1FEhYNgCHVOHt8IDgz"
    "L/KS1eEvMkQMMxGOVJUUmkby1lu29PUf/6D3cNOa9ZyoaKquRpM5FUVXfeO2pq2dWObUZCoSiqy+"
    "e2tI5BWiG2n1Y805g4gxxok8o9RQdQQhEpBVHghjaZMIHIehVYbUtKqmkmua6vsPdKUicUEQrJxk"
    "Vr2JB8MsrT7+8FcffvDB5W2tvYeP5ZSV85JNS6qIg02f67zq0ds8VYW6mqKEII6z/IxDNngRMESI"
    "+WlZAiwcFjRodaukadowDutGSNM5CKkVO6SrGjGMO67duLahrmfvO+l4SpQVanVpbmJ0rLak0Otw"
    "XHv1JpsshYIhQZGMdCqrwLv5se1Lb11HIDFUFSC0WO45jouHojJEVQUeVVUXvcdlKWvhYxm3YRHD"
    "ADoTTo7G4gPhSEZSGfwQmrqOGdATye3XbF5TW9392t6ZoRGbpETngsHzExvbmimlyUSCMSYoiqEZ"
    "hUsrNz+23Vni0xJJ3SRWc7ViQXVDF0VhYni8xG2385gyYNmUywBhjBFCpmZQxjieg4zgoG68NxsA"
    "mUgtXgohMDRDNLVg1yHb2s77b76xKNvzynuHps6OJeOxjU3NNcVFqq5lnsWJWPIU566+7/OaqZma"
    "SgGs9TrrPB4ZY4OYI6l098lzo4eHnnxgS/fgOMTcJX4YsCI1PzmdU5CvplKQQ4IkUkoxghBAbvGK"
    "S8pNhaNeX07Byo44MQx/cNvGdUurK48MnPG47a21VaapW0mgmdk5/PbbNn3rx09TU5dsUjQWb/S6"
    "ryktZgAIAMgATM6FXn7mtbtaqtuayn6z94giy4spzxgTRWF6dGJ8aCSvsDARiCiKwgk8yUTmwhUX"
    "UjFjIEIjo1oy/aNnnj0/MgYBq66u+PpDD2/buMo0DU3XDdOyPDU1uUvr8tZdVfP2ex/+/J5/vvvf"
    "HpbzPU5FhgAE4snB81NvfnjqxODIw6uWfvOmjnQwMZ/QbE6P5ZUsNKJ/ZmF8eKKyrg7zfHBqvqi6"
    "fFFEFywsh7HlYQkRbbbZwcGZo0dTsRjmxfz83MKCfLcrK2WYCS1tmRPK3C6lpia3rMzDDIOp2i+e"
    "+s599//r0zu+W7J59V6XPcftSjLAM9ZZWbjzzo0NBR5gstlYbDQQKikqoYxKojg7Pn3+9EhlTVVB"
    "ZfH8xDTRNF9RPjFMyzEu8jN79ryvME9xO6f7T/W88FI6mV7a1u5xu/yzs7qmSxI/Njqa39Gup0lZ"
    "uWdZWynHAZapb8w0fB77rr8+NdB3emwuGkukOEUssgtNOS6HUwa6riVSYpbz3cHRYEJr9rrTaXVs"
    "6Mz8tL+quS47L9vQjPPHTuaVFPGKrOppBBEGGSvunw94C4siM7O9L76UTsS3bNmKANmzZy8WZNnt"
    "Ojs3+/q+fevWrvvBd7/e2lyEAKE6u2CWM7kEdLOxsbqxGQGAADXB7DRQVTMzIAgCH/RH/9xztq65"
    "MTA9f67vFERc47JmwaVAQRg+0G0k0kWNtYZxwVJiyzUixPHYNM2Rg4dSoYXV6za7nMrvnvt9SUOT"
    "p6AACRgBFA+GBmKxP3V3d3RsZ5qGkJXWkMdAFKFhAE2nKXWx6FnuUTOQ5fOsqgYFvuf4SO/IdJ49"
    "NnV6SBKkzrtuxaLICdyZA4fO7Hun8547ocybmmpVRwAs14gQ5EUhNDk5e3pYcfiaG+p2vbqrtLnF"
    "V15KIdDTWjIWb96y8b4nd+7qGdx36DhSZEIowDgVjAy+vGfmxBAQBAgBhxCHEOZ5KMmMWhrlADQ1"
    "un5dx68e+1Lq3LnI+XPBs4P+sVGGwJFdr/f84fnazuW+2nI9nV5Ek2Eok+cuT9b04eOJcHTFig7/"
    "wrzOuOyiQl3TqUk5Hq/+8k1FbVWiwNd1NOw+1Hfd+vbMKEN6fvaCHk85SwtAxl1dqr7I7mAIU2Iy"
    "BLE32zTI7hdfTgZm/+GJfzw9dGb/S69Kzn1uUa6paUikktQki2NhZjy4IGqgyEo8ECS6XrukduhU"
    "n93nYcCyjqLTtv4rN2dXF6qJBOdAssueOB8Blu+G1DA99eW1N26Qslwglb7UwCmlDELO5bS6hyhN"
    "jIzfeucTvT1HWttXv3/wg0AoCIlJYvFr7/rCke7eCICyoixWP0oIMwi2GjghriwXJAaELNvtPqEa"
    "ot1mqobida1/8BZXoSceSzAIBAoWJmc3F+YChClVOYyb77wJ6Aa7DI0lAEUGHA9SyWAoMnJ+5u57"
    "vjU1G13S0Opy2HielhR6p8bPJlXzmad/jTiMZfHtp35tz/Y5c3zO7Bx7ttdiiFGKFdlbUjQ3dDwa"
    "iSJOQKbBYa7z3pvcRdnJRKzK62n0uMZC0RdHJrd84WpAzEUXwVQNZuazS2igIJzsO/2Hl/adGh6f"
    "m18I+qNud26VlBUILqxoa/ni9lvefOPND/v7fQ4nSMR8BdmpZFoPLASmJybTGqFIcDgygCA0iFGx"
    "YvnkwFDN0oahkTPjgWjzIxt8tfmpWHx9aVGj15sD4e693XUVJY1LSpiqW1n28WHSKt4CnhiZ3PHA"
    "911ZOcUFhYWFFXm52eNj4wv+0NLm2jf2vLH7zT2GoTfcdP1Y7wmPx+3OcjmcjsX5OGPQ9FQydUHU"
    "RDds2Z71D+7oGei3y47SNYV1m9qDoVC2orT5fAoAz3Ude62r73ff2A4hspL540bdaoiAcaIyOjZl"
    "mtzWrTeoSdXjUWpr8oaG5Fdf7Vq9alV7a3tv79EDB7sG9h/kKHXVVRCTXJqyIYSY590e10feFIv8"
    "8Ac9R/a++5W7rmeQDH4w4LLbAY9PLSz8fNe7v/zj/idvv6qpIo+oGvoEGkUCPE8R19t74qdPPW/q"
    "GtNJbo6jtaXY6RCqygocNhz0hwg1V3d27Hz0oXVtLdAwBgeGkokUL/DcxQ0CY4wQYlVqq8RBxAxz"
    "4dzkv9yz5fM3tgbTsSf/8tbJ7n4s4F8vzBfYXb+8/arVZT4zHOVkCSy6KmtxRrDL0X/s1A9/8vto"
    "LHXk2MmiNcvWPn7T7//nL43VJa6sDVXlJbIiSwpvLbkQiKWTHIeuvv7qtvZle/e/dfzEMZ/PW1JW"
    "KkiCYRqLDR4zw0A8z4D1dGCSghwnicfvvaplzZLSt0+e03SjaUPz6iVFoojNRBryEgSQMUoIxZKA"
    "7c7hU8MPPv4TxZbVf+SkWOC9/Qc7Q/7wjG6ee/WASc3SfF91dWVa1TLbEWiRwUAilXBkKdvv+EJH"
    "R9u+vW8dP3KisDgvvygfccg0TZRcCCDMWYkmCADhQDjBybZ0PFVd4PnajZ3fuGX9xpYqESGiGdZI"
    "hCAhJuR57HKHIvFvf/snazd/GUKhs7NDM8xND9w1eXbslR89W79xtae61G7zhqPkjy/tNQ1OVmRy"
    "cVmAEDJMM5lOlleWfeWr9912683JhHq490TQHxGwiKP+YFZpkdUmESxurHnm5X23bV3ny/HSaMxM"
    "py39Ws4bWvVKEVG2FwA8NzX93POv/fw/X4gkTDWZKE7FW1tLSisL+w72io6sxs4Va+++7kiRb/eT"
    "zzz+xKO5OfmIg5D72FS+uJKzLDYEy5a31dUv6TrU3dV1aH5uHvnHJoluIo7T0mrDhpXTQFh54yO7"
    "936oQU5wu6Rsn5jt5bNc0Gab0c1X/rT3ni99q2Xlnf/0vadv2npzXW25IODxiRkRo/vvuTnWNxye"
    "mKlcVhULhwobqkNG+nBPt9uXxQl8JmSfPBBZsJLpJMej666/+tGvP+R1u3EiFkunUrLDTkwCObRh"
    "xxf79x64/YmfFWc56isKy/N9iEMLgcjA6NTEQiAc1wsaG1z1S8oTelN9w66/vNra2hQKhQdOjXqy"
    "lPUrW/Z3HdVTKUaditdVs371oa7ulo42Sz5XwvkIFiKMRWORnPzsFStWoKrsXP/4FM/z1mBEraqw"
    "fNt1V+98wLay47gKXzh+7rlDg/vGwkZpZVqHa++45YadX/WVl2IO7NnzxrJl7S67vbSkOBiIHzzQ"
    "7/dHUtGIFXyOS8XiVSs7grp+4ugJRVH+zrZpcTDieT4ajgwODuB8t6Orv7+iqcHaVGJuevS8KAp5"
    "FRVVK5cLqyGEiFAiy3JoZj4wNWfPz4uHo6ahk2QynU7Nz81G4vFNmzcrNncoNCxLCgc5XTMQhwxV"
    "x4qtcvXyt996t3HpUsRZ+fW3DqXUZrMdPtE3MDCIJqem/cMjc6Njst0GAAvOzIycGAxPzVrdRDWM"
    "jAOZPz95+PX9RU0NNq+HEsJzeG5+fnp6Gud5kawYmuZ0u1rb2pqamjgAtFSaMWB3OxhHq1etjFPQ"
    "82GvLMmfQRKE4XDY5rBjHuGSxvqRvkHTMBEEyYVIXlnx6e4jnsJ8UZGATuLhUHB2vqC0rLyp3tB1"
    "QZYx5ucW5u1ZLkElqVCU4wVKTEmWBMxjCGOzfsRxVockhFPkpus2v/XKa63tLaIkWCXj07aXi45j"
    "enpGlmRrnegsyF7S0TZxcvjc0VMF1VU1HW0Na1ZwIp8IR2ORiGC3L920tqqz/cJYDqGhqqIo6Zpu"
    "RmMer0vXrGZCCEGYkyVRjVvrCggBL/FqMlW+vE3M8b2z/x1BFP/OHj0YDE5OzkYjcZyVlTUdCLR+"
    "7lq7L4uZTJSktK7bPO6qbC+kjCBriccMYhiXdigMYcwY71Dk2rqq4eHhZDLFrAGZQYxcbmc6kQAQ"
    "IA7ZPPboXAwg1HzDtd3P/nb12lVuj8s0LpB0CVzmvyvCwMkBwFhVdQVyudxaOGpa6wHIybxBDQgA"
    "MU1N0zRDNzTdSKnmR2gsioiqMUYUmwwxgBynG+rFnQB0OOz+0UlIIWNAVkSIgZZO5zUs8dXX79u9"
    "T+SFi9M6FEWRzxybzRaNxrq6unMLcrOynZjjkJZMMUIsA0HphfdefP+VAYcQmLq1PeYwopRJkhiJ"
    "hA3DsFYDhOYV5A+d7DcT1kzDSyIviqZqUkpL21qOv/DSxNhUXmEuJSSZTE1PTzmdLms2JeTAgYOm"
    "aXhzfKqm/R/I2ufkj1YKuAAAAABJRU5ErkJggolQTkcNChoKAAAADUlIRFIAAABAAAAAQAgCAAAA"
    "JQvmiQAAIKpJREFUeJydegdwXMeZZne//CbPABjkHAkwgAlMEklJlCiRytGKtrS2Za/PWz6f09Xt"
    "1d7e3q3ss6W1XbbKlmwFy9YqnCxRgZQo5kyKCSASkTMGGGDyy9191TMklUitfF1FFuoBr9/ff//h"
    "+7//h088tQtceRFCJEmmlJ5tP7N/367I5ISkKt6igmBJWaiyWA24JZcXyTLlEcUUfuJFCCHB2LFt"
    "UWavg/+PRQHkEE6njrz8eqC8smXTNQgh4mCKKKGEl8T4wMj+Z56DVzoApRRCKMvy6Njw9ne39/f2"
    "e8OBytbm/IZqd2G+KMoAEIcSQgDA5AoSMMEhk4T9DyD8m49AKS/y+lz8ow8Puv3+1qtWcwJvUyen"
    "KoHjDr34En/ZNwkhgiAAAHbs2LFn1/uCoiy/dVPZ8oWC1+U4DrYc0zTBlcTK6hsiBDiE4IVfU8r2"
    "zP3qyy8IIXaIJ5y/ZMOa4zv2dn10evHaNuCwDzPlIFS7uu0yByCEyJI8H4+98X9f7+nsrlm+cNGN"
    "G10FIcMwzbTGRIJsXemriOMggti0bct2LBtjCiHhBEFSZI5D5G88gJbJpGPxwuKimuam0d4+PZHm"
    "fQomDoTQse38iir+MtLL8uzs7HPP/T4anW27/caa9W2242TSGaZUhL5Ah5RSxHF6OjM5MpGMxojl"
    "EEAhgIQ4lFJ/QahxyUIAyGesFFx5N57nzYzW196p1WfKFtRNDQwNd/XWr1vmYAdCZpmEgx8fgLLF"
    "XHZ+fv6FF55NJGPrHr6zdHGLqekUICb9f7QQQpaudx4/wyEhL1ygqi5RlSECjm07lgU5niIOMKe5"
    "8McQIew4X6B+x3FCBQWp4thwT7/H669saRw4112RaeRkkZILmuQpJZQCjuMEQYQQptKpv/z5hVh8"
    "7qqH7wk11+spPSs6/JJBwzbM/HB+aXW15JbIhejDLiGnIIyZuJRSjudT8zEjY+SXFBGKv2BXAmh5"
    "fU06pQ33DlbVVhJA03PxUHmxja2c+/GSpHAcl0olBwf7/P7AO9u2DQ70bfr6IwULGo1kBnHcl/U7"
    "CAjGLp/XGwpQQk3LuqxScz9wEGrJTH9HH8CgsLrEsqwr2RIhRBSF6qa6juPt8XjSo7ojA+OhstJL"
    "OuXPn+/q7u7q7elMJONuRdUyxlUP3F3Q0qin9b9B+ouLAuDYzhd5efZWLNsuKC0yMuZw/4CsSv78"
    "kI3ty8ZZCKHlOB6fr7C0ZGYiwiHOyGSAw5yYZuMzhHILR2koFDIsR89kVt51W826VXoqTbkv8tcr"
    "n4B+Xg5mStnnELEFIGTuSymwccfxM4CCluWtQGIPr7Al5RGvpY2eUx1ENxxDX379Rk4RCHUggOi+"
    "lcvXr1khSEImHmvdurlybVsmnaa5aPPlw3b2L5l0PJ8NBhdezP3M3MuliqIIHEuPx7TZWSsZB46p"
    "uNUFy5bohjk2OIwQd6UDsGxAHNWn+guDmFDbxFYmax3ZP+fvXbL0n/d9OD4yvGTzDTUb1xqadilW"
    "5kyIXowbX7A4gUcAZtIZjB1vwE8IJRgTQnlJQgDEI5Gpc91zQ8NabN5K6xRjJHKSS/EWFpYuaC4q"
    "KZieigQKgv78gO0w87uMftg/HC4Jx8dn9DjJpNOeokDuOX92bLS9s7tq5cqFt2zRdONS5kQ8Nz0y"
    "piiqryAPO/iK8ZpSXhAiI2Op6SiBcLR/oKaxPlgUVgM+URKj/QN9B45EB4dswyAUUIKZQhGHLKgn"
    "MomJ2ZGP2n0V5b6aukwy4wsFrgQ3WEjF2OP2uN3uVGROUmXMtsoe4PmjB93homW3b7Usm9llbous"
    "j8yOTgUDgVBhAc6l78vKn70obT4xNzx29x039yhK+7me2eFxJx13YvHYzKxtmpQCXhRdipwVwCNy"
    "yDCMeCo5F0vyEGamZuyUxgMSLMgTXVI2ql7uW5R9SA24AU8Vj4sSlgQpBPzQzPy6++9VAkEtlc5l"
    "KxZAsjCMEyQCICT/cRZwIPCr6lJZartqtb7+quf//fV9h4/wHDJtGyK4qKV5w4arGxtr8wIhCjjb"
    "yui65hA4NDr2zrvvRefm5+KJ87v2yUhYePO1huZAdPkPOhgHC/KHINJiyaCn0MImBIAvrK4qWtyc"
    "SmdywItQShwCIFU4XpTEVCaFMYEQZWPW5RekFDtOEOPRvQcqNq491tl5fM8uSebTaX1hS9N999y5"
    "cnlrPJHoONfz3js7hkbHE1k38Pk8G9atffzvHvUHfIeOHD3V3tXbca58xSJ3KOBczhNy+Nzj86qq"
    "Mj06Fqgsyj3ma9euVlWXB8GU4yQtHJKkUlV2KB3UdW/IN9Y9n0mmlICHOvhKF0EI0dMZ2zLzy8ve"
    "+HDXK9velhXVzGi3btn87W8+Fp2PPf37Zw4eOhFP6RRQSVZFWYEQzI5Ndv3hRVUVV7Ut/dr9X7n2"
    "mg3fePwfBg+dWHbnTbZtXzaNsOCLkORxpWNJYDHjYFEo2FjX5FOavB6N0H3jkWqfusjvG0ylu1Px"
    "UFFBZHhyemKqJujFFxHBp7dkGMRMZ5JT0dLy4tOJxGt/fdfj8Wi69tDDX3n0gYfffPOvzzz/QkbL"
    "5JWUl9SWyi5VVGTE84BSPZOhgFqWvnPXvtMnTj7xxD8ubKnrno1Qx0HMAa9w2xAGi8IDp8+Zus6p"
    "Agt0RV5PheoybEfm+TyFd/E8YbAGUAxESQxXFo/2DRVXlCpul4Odzx4hi8s5jnO5XYl44pV9+xVF"
    "Tae1B++/76v33//07373yquvufMLFixaLHsDLApRtrdpmwjC2vVLi1pqCquL5gfHnv/Bv77w/J/u"
    "vO+2wbf3puNpyevKBpnPXwJ1AJZdKvMH2xKgSABBXoETIWJWn438XbF4RyLVPp8AADm2Ey4p5CDs"
    "PPIRJJcvqSgAGALIo76u3mQqbTp4YcvCB+6568XnX3rl1X8vqqmpXdoquP225WDiEELMjOYvKlj3"
    "2F0r799c3loDZVR1deutP/zG6f7JvJCvrrRwPhaHCF0+p0GAKXZ53QCAdCIFEQco4AOKSBFhKJ3p"
    "HY6kzUnNYrGaBSmIHYc3nNmZ6EhPX1VLo2mZEOVAyAWj5DlO03QzmQm4lOk57FjmfffcPjU5/pfX"
    "XgmWVhXWNjisGGPw02bwjrbcuKrumhWyT7UtHVgEQpiIzjesX1F84PTAxHh5wNUeS18x6EEICIUC"
    "hzjO0vXcDfEq5Jh5s8BJddvhOSRkYw7J1hOJaCwZjxfkBQfPnlNUOVxdZZo65D7+BEIoNT1rmcZ/"
    "++63d7+//Vx3f+uiRdveftsmoLS6hgAeEAdQYKS0QHFe2wPXh5rKWalmGRAiwD6UdU2JD5YVjU7H"
    "HQyww6qfLyigWR0BIbXZxbMohFn2YsuhIGk72Rr8whMIYSadhhz45p1bXn9v55lDR1e5Xb7CfMMw"
    "0MVQTSmdGpsozssrKwh95d67dNNBCI2NjEqKIqluk2AGHHiuZeuaho2tsk81NI3Fa/ip8ggCYBPi"
    "dvs8snh8coBQcsW8z/44m8E4DgBEAUQJ0+IhkHl+SteihsF/omiEEOqaxgEUkuVH79ha5PWf3nMw"
    "NjUrKSpghSLhBD4RnZubnlmzpFmACFDgkkXHtlOpNOQ5Zpm2I3uUDY/f3nrXBs4lmIbBcZApMLdA"
    "VqMcp6cy8em5qvyAT+Vt2wEXq63LeQEglFBMACaIshOg4ZQ+nTZjltkbT0PAXbyPi/q1HA5ACzvh"
    "UPDxu27NU9SP3t8z2dHLUShLEg/Q0JnugKour6+zHBa8MSEIQk6SOY430kZBXekNP3gwWF+ipTMU"
    "U4Q4GxPTsU3HMR3HYaCPCAIfn4pqiUxLeTiV0HiOQ0zLn1V8zqjYJ2zHwZhVleyiCJ9xyIHILIDE"
    "IowF+RTypCCdzoQ9stPdHZmeqWxq/MFD97y0fefRA4cLhkZrFy2Iz81NDQzftWlD2OdLGzp7n+Ua"
    "6A8FiUNLW+uW3Xu95HdbhsYhiJk3O6UuV5HHLfKc6TijsUTMchCm7Yfby4O+5fUlO091ZX0UAfxx"
    "nZmteBG2bAooryh6OoMpUT0uwjwa8AhCG1BAOAQ/QRhkD0sxBg62UunY8ESB20UI9sry47ffXFVa"
    "tPvY6ZN7DnGQrl++9KbVbRmLSc/KX4iwQ4ryQnoqU7Gkzp3vT8cTnMDYFJHj1peV1PgDOfOngK4o"
    "Knp/fPLdV3f2n+p/+vGtPhGOTsUlUcwV0xdhMUUIYcvqOt1RWFJSXlupJVMIIkVVs64C+azFsCuj"
    "l6EYzPTs/JKW5gXXXJ2ytJnpaZ8v4Ha57li/dnlD43Q06nWrRaEAojbOKp7VWDYOhuC3//721954"
    "68jrO+qvWcGLHMbYJnhdWUmjP6ARFjplCEUAo5p+6sPj+3Ye+8ralptXNGRSxrSmqy4fw5QYMw0C"
    "wAwSoaHzI+nZOCorgxQlpqKqS5VcqoUxAp+gVT5lO4QiDmZi83Y8lohEfvqrX4+MjRkZw+t1V1aU"
    "3XvPvUXhgqKAl1Ji2TZhyRjZNkYcam0tq64ICh7PL372nx967J92PPWn9Y/cqnhkw7J9XrcCGFvn"
    "ADAWmT1wsve9410jI+PfuWblj+5YxQE6HEmkTcebr1y0HAaYBZ4f7huOjEVKqmoKigrTyWR8Zr68"
    "oQYKHLAYyL/8AQAAkir3Hzwc7+vbMzxg6QYrcRDUDH1gqEdx+b/1zUfTqRhCPLMcSg3D9vtdS1sr"
    "CsIuatpOKvHgQ7fuO3j22X/7w8ltexffsr54UUP83EBjKDSTSPeMTvaMztg2WVFR+JP7r722qQKb"
    "BqVoTjfmUlqxz8d4nixHhiAc6BmYHBgvLi4ub6yEPD83NIZNM1xWQnKELAT8J+MTo3+zL0uy1PXB"
    "7smObl4WNU0PBkNVlRU+n2tBY3NleYU/FASsNOeYOh0sK/ySRRUlRV5J5YnhIAQ5AKhu/O7ffnzV"
    "2iXPPPfXiVPnZ8ajybQmezyh4vx8j/pQa/2m5vLmsB9gE+sGIZSTuJ2dAxnLUtwqJoTjeUjIYGff"
    "1NhUeUV5VUONA6llW8Md3eGSYsnvtR0rVzYwYIh4TkukxnsHahY2caIgKWr79g86d+xEgohte+PG"
    "9T6v+3x379TEeE9PXyjoLy0pvfGGTXW11fH5pM8vr15d7Q8owLKpZecSXLYkciDgHv7q7Q8/sHky"
    "Etc0M53RBZfsgzgPEVkWgYOJpuUSkyAg3dB3nTof8vh4WUIEp+KJka7+ZCxV1VxfXFliWo7AS0Nn"
    "OoxEqnjl0k+m6QtOjAlJJNOmYft9vp4DB7p37xYkEUHu8a8/NjU1se2tt7ANFLdLdLuGIzMd3T37"
    "Dh68/95777/n5qoqn98nY938BBWdu9IsHkkkEAeLC/wsiuey79wsSCVtLcN4VsheYAFAEN/af27W"
    "BKuuXmHqxuT5wdH+AUX1NC1d6A3nWZYhyUp8OjpytrusqtJXXOBkC4YcJONzrB/ieMRzEHCWbvTu"
    "3Y8oMC3ze9/9bmR6+rVXXvKGSvJKKtx5Pl6UAECWnp4aHX354MGW9c0br9+Kk0mOJfZLYfsCFoGI"
    "7cqesBCegwGQWhaigIcXSBTKOAF+NBL/+bsHJUkc6u2bHhpLRKJFNZXVSxa5fF5LNySXqs/NH/3T"
    "y7ysVKxcZFOHQBbKLtwAKwkB4HlEHWw61syxM8ZszCJk44arMTZfeum5UGFVYUODoKrEtm0HQ8Q+"
    "WrN0masi/8mXP2iqLL1+7UKS0RhjlWNIJRHwDOgCw7xwktzXGPSCRJSBYV6M3TBLMWOe5/xuz1w0"
    "Ndnek4xOa5E5rijfWxA0UhnF554fHTn4x5eJZa2+62ZBFe1P85AsbQNKBUHgRMFKZ2b6hzAFiqJc"
    "vWb1W2++5fbnFzc3Cori2DZTKURWRi9orLjmu/dsfOhGXzjvt3/daTOMnZOeAh7N9Y2cffGtE394"
    "TUtlAGIA6VNLVjBEIEcUYFblYU4Jl5Vue+r7X79hHY5Mzw8N8Yh0Hz4cmxgX3crwR6f3PftCcmqi"
    "etniQEO1ZZqfqTZZFKKE8AIfyAtFevuNaAw71rKlqzQtPTQ0VLt0pSDJFvNODhBqmebi265uvmmV"
    "Q2wkcG2b2va9ur13aLKltoLoBpKE9Mzc8V/9GSc0T1O5IEpZRvGT+YVCHiG/H+gGoZiJIstUVHif"
    "r/fE6V888fPJ8dl77/lKeVX1b3/9m6MvvCL7PePtp6vqFwRkNTI52WRehgPO2mg27yqykpie0Qwd"
    "ULpk8eKhkSFOlFxev2M7HOKIjQGCy+7Z3LJlnePYxCIYA1/IZ1MwMRMDfDb+8nxqZs5MZVb88JGr"
    "f/INXuQ+T04yX1ZdKBhEwRDwB6HLzft8L7/01vrrHp1PGo888rXFi5YhKq1sW63NRZMjwzdsue17"
    "//ADDkq8IAiimEuyNIsCs86WvQGWAwh1+7wE21ZKExW1vKLsxPEjnCpDUaAUYEyoiJbft6n+qsVa"
    "Ip3tTTDDNh0HEeB3qbk0Byw7VFm69iePhWrKoY2zyv/UAbIGz7qCLGTxPGT8Jv3p//71j//xN4Io"
    "SwC8+vobqeQLgNqirEiyhDh+sH9g3759tmMjKpvJNEKcoEggC1sYe2k72USWLR0DwSCi2LFNSVJ8"
    "qkIxlRQXg4GmAzl4zbfuzm8sSSeTAHIWxpRQlyiO9w35VaW6JAysLBFCiKjI+Q3VwLKZtXwGEmeh"
    "AccqWggMY25uPm2T5//413/6nz+rqV1iO7iivCQUCKiqWlxc1N3T/f4Hu3meHxoa7O8fYDE9Fn3v"
    "X3+hBoLu/KAnL+QvLHTlhTz5eRczMaVAQPmVZTM93TbB0dk5B1NBUBBAxHbaHtgSbqow0mkbUJ+I"
    "6gN5AYnrn0t0H++8vW1hfjhIMpmLUYjSnKV+XnqBdxyy5739ew+e7hscHxoZT8zFZ6KJtes2a6l0"
    "/8Bgc2P91evWLVy0gBDnnW3b1JA/WBCe7R+sbCwjAJimiW1HTyejc9PjmmU7RFQUV/DiASiEFrYq"
    "ly7p23/MTiX9waDb53Kmp7Vkuv66FdXrmvVMikDQFPSvLilyc0IIgoHOobRmbV3b8hk7+byfMWPl"
    "UCptfO9HP/1wz0c+T1BWZY87UF/dyDFOwJwBqKm5fs/e3dt3vNnWtqalpfn80OCGxx4eOHZaEoVQ"
    "OMAI8qxWWBuPUtuyDM3SdT2dTF/CQqzScefnNd94HTC0strqUMCvpfXClrqW29ZZhuFQWu7zXldR"
    "bhHCQ3B8fPqPbx64bvXSFYsbqG5cUP8VFiEUqtKxXcc//PCjTTdtqquu5hGfrbBoOBzYs/tALCbc"
    "dfc9W7dcf+jA0QOHjx47dtSdXyS5XPGpmWDQRzB0nAv9m5x2BEGQA4I/6KYll0wo18wx9MaNa2YG"
    "h//Pk08tqq4KNzXUX7tCdstaIo0BrXC7BQBEhCbmYk/88U0KwX/9yiaRtf9Zw+fzcjNtZXsLBBMB"
    "CdPRGAaosrwCImqYZiikVlUGC8IB22w+f34wGp0PBvxbtm5pa2vbt//gyVNndv/+RVGUAqFCVs3l"
    "YMjFj1BKbSeXIukn4DQFiOeT0ei+P/7Zx3E/fPC2fmqc3n88XFWg+lzQsmMApyge6R/72Z/fn5lP"
    "/urRLfUleVjL1WKfk54QKPCc7AGAcpTs3XPk7e37LU3DluPyBahsL1lSIfKshK0sL84LurRUJuD3"
    "ZTIpn99z5523rl61cvt7O7p6z/efHyivKPWF/DTbRLykqUsjAJ/oEwPKCXwmMmelrB/95O+2Xr+Q"
    "qODbz2x765m3Fq9u8YYCu9r7/jIw0t05XllS/Ms71q0rCjipNCcKnw/2BBOkyom5xBsvvKMZ1sTU"
    "zDN/2iapUstNV/WODKeS0RWrFsiKSB2cLcyh4pFYPQsB5aFFbMOw8opDDz/6UFfn+V279pzr6A7m"
    "Bysqy91uN6WEsY4fawxmbfFCP5fF8vmJabdL2rSmAdjaLSuqIbj5qXcO7X3jQ4ABBbgo6L57ScPf"
    "X7+0PM9jxeJ8AEGWXD4+AKXUcbDg8/T1DHzn+z8/3zeOKBgbnyhuqLrrpz8sWlD7xn9/8i+/emXx"
    "vqZbtq5d27a0tLhQdbEyTXesC5ErS7tkuy1w0aLmurqaw4eP7N+3/9ypc0UlxeGisOISHcZSXujy"
    "89jQeVm52GBjj0SOY6ZLgWVaN7c1XdtSdXYsMpfKBNxqS3G+z68Cy8KGLSCeYEyztN6FcQ6MOZEX"
    "3N7Z6Zkf/+OvZ2PGjTdu2rljJ4Jwxd2bS1pqJ84PhRvqveGTpoZ7+8Z7OvsbG6qrqiq1jOn3fYrN"
    "ygXitJHieHTNpg1LWhfu3bP/6PHjMzORsvKSvHC+IAiYXSDk05ORvPoay7KznQqqen2plJY2LFbh"
    "Uuhohsqj1fVlgIOs0LYJzrA+GmL3BpHA0DhzVkw4gePcvkwi8cunnv7Dc2+ldfOmm64PhkLR2fmW"
    "a69qvW3TwOlzu158GyKhum1p34Ejt1VvxrZ59mz3iY+6bZvU13kvTQ9cWijL8ma0tNfrufOu25Yt"
    "a/1w555z3V0TUzOVlRWhUBBCwMdn5vIaa3PaZy2Q/KBF6METnWvWt1ppg2MEHKUmo2izcPjjmQkG"
    "RxjjgTlJ5ETFSCZfee71nz75XHf3mOpirFco5KuqCfMSP59IRKcix94+KEveqx+4vqyxetv/Uv78"
    "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"
    "L/8KHwvw9v8v7y3474EFhukAAAAASUVORK5CYII="
)


# ShellExecuteW 提权启动返回码：用户取消 UAC
ERROR_CANCELLED = 1223

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800

_TS_PREFIX = re.compile(r"^\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)?")
_DURATION = re.compile(r"outage:\s*([0-9]+(?:h[0-9]+m[0-9]+s|m[0-9]+s|s)?)")


def humanize(line):
    """把底层日志翻成人话。返回 None 表示这条不适合给普通用户看。"""
    if not line:
        return None
    line = _TS_PREFIX.sub("", line).strip()
    if "Auth OK [" in line:
        return "认证请求已提交，正在确认网络…"
    if "Auth FAIL [" in line:
        return "认证请求被拒绝，学号或密码可能不对"
    if "Network restored" in line:
        m = _DURATION.search(line)
        spent = f"（断网时长 {m.group(1)}）" if m else ""
        return f"自动重连成功，网络已恢复{spent}"
    if "Network DOWN" in line:
        return "检测到断网，开始自动重连…"
    if "failed:" in line and "Check #" in line:
        return "网络检测失败，继续尝试…"
    if "Network check failed" in line:
        return "网络检测失败，继续尝试…"
    if "Auth response received but network is unavailable" in line:
        return "认证没有生效，网络仍然不通，稍后自动重试"
    if "Service started" in line:
        return "开始守护网络连接"
    if "Continuous mode" in line:
        return "将持续守护，直到你点「停止守护」"
    if "Scheduled mode" in line:
        m = re.search(r"exit at ([\d:]+)", line)
        return f"守护中，将在 {m.group(1)} 自动退出" if m else None
    if "Run duration reached" in line:
        return "已到达设定时长，准备退出"
    if "Service stopped" in line:
        return "守护已停止"
    if "Run duration reached" in line:
        return "已达设定时长"
    # 兜底：已经是中文的自定义消息直接透出
    if re.search(r"[\u4e00-\u9fff]", line):
        return line
    return None


class QueueSink:
    """日志出口 — 可能从任意线程被调用，只做入队，绝不碰界面。"""

    def __init__(self, target_queue):
        self._q = target_queue

    def __call__(self, line, level):
        self._q.put(("log", (line, level)))


class StatusSink:
    """run_detection_loop 的 status_callback，同样只入队。"""

    def __init__(self, target_queue):
        self._q = target_queue

    def __call__(self, status_line):
        self._q.put(("status", status_line))


class GUITray(core.TrayApp):
    """场景 A 的托盘：复用 TrayApp 的 Win32 实现，只替换右键菜单。

    core.TrayApp 的菜单面向控制台窗口（Hide to Tray / Show Console），这里换成
    「打开主窗口 / 隐藏到托盘 / 退出」，并回调给 GUI，而不是显示隐藏控制台。
    """

    def __init__(self, config, on_open_window, on_exit):
        core.TrayApp.__init__(self, config, start_hidden=False,
                              status_text="待命中", on_error=self._report_error)
        self._menu = None
        self._item_status = CMD_OPEN_WINDOW
        self._on_open_window = on_open_window
        self._on_exit = on_exit

    def set_status(self, status):
        """可从窗口线程调用（只改属性 + 调用已线程安全的 _on_status）。"""
        self._status = status
        self._on_status(status)

    def _hide_console(self):
        """No-op override — and this is important, not cosmetic.

        core.TrayApp hides the console for its console-hosted modes. That helper
        also calls EnumWindows and hides every visible window owned by this
        process, which in GUI mode includes the tkinter main window. Leaving it
        enabled made the window vanish ~0.3s after _show_window() (observed
        state went normal -> withdrawn with nothing calling _hide_window).
        GUITray must never hide windows; the GUI owns its own visibility.
        """
        return

    def _report_error(self, exc):
        if self._on_exit:
            self._on_exit(f"托盘工作线程异常：{exc}")

    def _create_tray_icon(self):
        """Register the notification icon with our own .ico, then build the menu.

        core._create_tray_icon hardcodes the stock system icon, so replicate its
        NIM_ADD call here with the app icon instead.
        """
        nid = core._NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(core._NOTIFYICONDATAW)
        nid.hwnd = self._hwnd
        nid.uID = core.TRAY_ICON_ID
        nid.uFlags = core.NIF_MESSAGE | core.NIF_ICON | core.NIF_TIP
        nid.uCallbackMessage = core.WM_TRAY_CALLBACK
        self._hicon, _src = self._load_icon()
        nid.hIcon = self._hicon
        nid.szTip = self._title
        if not ctypes.windll.shell32.Shell_NotifyIconW(core.NIM_ADD, ctypes.byref(nid)):
            raise RuntimeError("Shell_NotifyIcon NIM_ADD failed")
        self._build_menu()

    @staticmethod
    def _load_hicon(path):
        """LoadImageW with the right types; returns an HICON or None.

        LR_LOADFROMFILE=0x10, IMAGE_ICON=1, LR_DEFAULTSIZE=0x40. restype must be
        a pointer or the HICON is truncated to 32 bits on 64-bit Windows.
        """
        try:
            load = ctypes.windll.user32.LoadImageW
            load.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            load.restype = ctypes.c_void_p
            return load(None, path, 1, 0, 0, 0x10 | 0x40)
        except Exception as e:
            core.log(f"Tray icon load failed ({path}): {e}", "WARN")
            return None

    def _embedded_icon_bytes(self):
        try:
            return base64.b64decode(TRAY_ICON_B64)
        except Exception as e:
            core.log(f"Embedded tray icon decode failed: {e}", "WARN")
            return None

    def _load_icon(self):
        """Tray icon: assets/campusnet.ico first, then the embedded copy, then
        the stock system icon.

        LoadImageW needs a file on disk, so the embedded .ico is written to a
        temp file just for the call — that keeps a lone CampusNet.exe (shipped
        without assets/) showing the right icon instead of the generic one.
        """
        for candidate in self._icon_candidates():
            if candidate and os.path.exists(candidate):
                icon = self._load_hicon(candidate)
                if icon:
                    return icon, candidate

        data = self._embedded_icon_bytes()
        if data:
            temp_ico = os.path.join(core.get_runtime_dir(), "_tray_icon.ico")
            try:
                with open(temp_ico, "wb") as f:
                    f.write(data)
                icon = self._load_hicon(temp_ico)
                if icon:
                    return icon, "embedded"
            except Exception as e:
                core.log(f"Embedded tray icon failed: {e}", "WARN")
            finally:
                try:
                    os.remove(temp_ico)     # the HICON stays valid in memory
                except OSError:
                    pass

        fallback = ctypes.windll.user32.LoadIconW(
            0, ctypes.c_void_p(core.IDI_INFORMATION))
        return fallback, None

    @staticmethod
    def _icon_candidates():
        """Where a side-by-side icon may live, most-specific first.

        Every entry is absolute or None: `getattr(sys, "_MEIPASS", "")` joined
        with a path yields the *relative* "assets\\campusnet.ico" when the
        attribute is absent, which silently resolves against the current working
        directory — that is how a stray icon can get picked up.
        """
        paths = [os.path.join(core.exe_dir(), "assets", "campusnet.ico")]
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                paths.append(os.path.join(meipass, "assets", "campusnet.ico"))
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            paths.append(os.path.join(here, "assets", "campusnet.ico"))
        return paths

    def _build_menu(self):
        self._menu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(self._menu, MF_GRAYED, self._item_status,
                                         "状态: 待命中")
        ctypes.windll.user32.AppendMenuW(self._menu, MF_SEPARATOR, 0, None)
        ctypes.windll.user32.AppendMenuW(self._menu, MF_STRING, CMD_MINIMIZE,
                                         "隐藏到托盘")
        ctypes.windll.user32.AppendMenuW(self._menu, MF_SEPARATOR, 0, None)
        ctypes.windll.user32.AppendMenuW(self._menu, MF_STRING, CMD_EXIT_APP, "退出")

    def _update_tooltip(self, text):
        core.TrayApp._update_tooltip(self, text)
        if self._menu:
            status = text if len(text) <= 46 else text[:45] + "…"
            ctypes.windll.user32.ModifyMenuW(self._menu, self._item_status,
                                             MF_GRAYED, self._item_status, f"状态: {status}")

    def _show_context_menu(self):
        if not self._menu:
            return
        pt = core._POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(self._hwnd)
        cmd = ctypes.windll.user32.TrackPopupMenu(
            self._menu, core.TPM_RETURNCMD | core.TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, self._hwnd, None,
        )
        ctypes.windll.user32.PostMessageW(self._hwnd, core.WM_USER, 0, 0)

        if cmd == CMD_OPEN_WINDOW:
            if self._on_open_window:
                self._on_open_window()
        elif cmd == CMD_MINIMIZE:
            self._status = "隐藏到托盘"
        elif cmd == CMD_EXIT_APP:
            if self._on_exit:
                self._on_exit(None)

    def destroy_icon(self):
        try:
            self._remove_tray_icon()
            if self._menu:
                ctypes.windll.user32.DestroyMenu(self._menu)
                self._menu = None
        except Exception:
            pass


class GuiApp:
    """场景 A 主程序：主线程跑托盘消息循环，窗口线程跑 tkinter。"""

    def __init__(self):
        self._q = queue.Queue()
        # 首次运行就把配置文件生成出来，用户随时能在程序旁边找到它手动改
        self._config_path = core.ensure_config_file()
        self._config = core.load_config()
        self._state = STATE_IDLE
        self._monitor = None            # 守护线程
        self._monitor_stop = None
        self._auth_thread = None
        self._session_start = None      # 本次守护开始的时间
        self._last_check_at = None
        self._last_recover_at = None
        self._last_recover_cost = None
        self._recover_count = 0
        self._outage_start = None
        self._tray = None
        self._tray_exited = threading.Event()
        self._shutting_down = False
        self._alive = True          # flips False when the window is destroyed
        self._raw_buffer = []
        self._log_detailed = False

        core.set_log_sink(QueueSink(self._q))

        self.root = tk.Tk()
        self.root.title(f"校园网自动认证 {core.VERSION}")
        self.root.geometry("680x640")
        self.root.minsize(620, 540)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._set_window_icon()
        self._build_ui()
        self._center_window()
        self._refresh_labels()

    def _set_window_icon(self):
        """窗口/任务栏图标 — 用内嵌 PNG，exe 单文件运行时不依赖外部资源。"""
        try:
            data = base64.b64decode(WINDOW_ICON_B64)
            self._icon_image = tk.PhotoImage(data=data)   # 必须留引用，否则被回收
            self.root.iconphoto(True, self._icon_image)
        except Exception as e:
            core.log(f"Window icon unavailable: {e}", "WARN")

    # ── 主流程 ───────────────────────────────────────────────────────────
    def run(self):
        try:
            self._tray = GUITray(self._config,
                                 on_open_window=lambda: self._q.put(("show", None)),
                                 on_exit=self._queue_exit)
            threading.Thread(target=self._tray_thread, daemon=True).start()
        except Exception as e:
            core.log(f"Tray unavailable, running window-only: {e}", "WARN")
            self._tray = None

        self._show_window()
        self._tick()
        try:
            self.root.mainloop()
        finally:
            self._shutdown()
        return 0

    def _tray_thread(self):
        try:
            self._tray.run()
        finally:
            self._tray_exited.set()
            if not self._shutting_down:
                self._q.put(("tray_exited", None))

    def _shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._monitor_stop:
            self._monitor_stop.set()
        if self._auth_thread:
            self._auth_thread_stop = getattr(self, "_auth_thread_stop", None)
            if self._auth_thread_stop:
                self._auth_thread_stop.set()
        if self._tray:
            try:
                # core.TrayApp names these _stop_event / _hwnd. Getting either
                # wrong leaves the tray thread blocked in GetMessageW and the
                # process alive after the window is gone.
                self._tray._stop_event.set()
                self._tray.destroy_icon()
                if self._tray._hwnd:
                    ctypes.windll.user32.PostMessageW(self._tray._hwnd,
                                                      core.WM_USER + 2, 0, 0)
            except Exception:
                pass
        core.set_log_sink(None)

    # ── 守护循环（场景 A 的核心，与场景 B 同一套行为）──────────────────
    def start_monitor(self):
        if self._monitor and self._monitor.is_alive():
            return
        if core._need_setup(self._config):
            self._log("账号密码还没填，已经帮你切到「设置」页，填好保存再回来点开始。", "error")
            try:
                self.notebook.select(self.tab_settings)
            except Exception:
                pass
            return
        self._monitor_stop = threading.Event()
        self._session_start = time.time()
        self._last_check_at = None
        self._outage_start = None
        self._set_state(STATE_PENDING)
        self._log("开始守护：将持续探测网络，一旦断网立刻自动重连。", "ok")
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def stop_monitor(self):
        if self._monitor_stop:
            self._monitor_stop.set()
        self._monitor = None          # 立刻反映到界面，不等线程真正退出
        self._log("正在停止守护…", "dim")
        self._set_state(STATE_IDLE)

    def _monitor_loop(self):
        """后台线程：只入队，不碰界面。"""
        try:
            core.run_detection_loop(
                self._config,
                stop_event=self._monitor_stop,
                status_callback=StatusSink(self._q),
            )
        except Exception as e:
            core.log(f"Monitor loop crashed: {e}", "ERROR")
        finally:
            self._q.put(("monitor_done", None))

    def auth_now(self):
        """用户主动点「立即认证」— 只做一次，不影响守护循环。"""
        if self._auth_thread and self._auth_thread.is_alive():
            return
        self._set_state(STATE_AUTHING)
        self._auth_thread_stop = threading.Event()
        self._log("正在检查当前网络…", "dim")
        self._auth_thread = threading.Thread(target=self._auth_worker, daemon=True)
        self._auth_thread.start()

    def _auth_worker(self):
        started = time.time()
        try:
            ok, detail = core.check_network(
                self._config.get("check_url"), self._config.get("request_timeout", 5),
                self._config.get("check_expected_body"))
            if self._auth_thread_stop.is_set():
                return
            if ok:
                self._q.put(("log", ("网络本来就是通的，不需要重连。", "ok")))
                self._q.put(("auth_done", (True, time.time() - started, None, True)))
                return
            self._q.put(("log", (f"网络不通（{detail}），开始认证…", "warn")))
            ok = core.do_auth(self._config, None)
            if self._auth_thread_stop.is_set():
                return
            cost = time.time() - started
            if ok:
                self._q.put(("log", (f"认证成功，网络已恢复（耗时 {cost:.1f} 秒）", "ok")))
            else:
                self._q.put(("log", ("认证没有成功，请检查账号密码。", "error")))
            self._q.put(("auth_done", (ok, cost, detail, False)))
        except Exception as e:
            self._q.put(("log", (f"认证过程出错：{e}", "error")))
            self._q.put(("auth_done", (False, time.time() - started, str(e), False)))

    # ── 队列轮询：所有界面更新都发生在这里（窗口线程）──────────────────
    def _tick(self):
        if not self._alive:
            return  # window already torn down — never reschedule, never touch widgets
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass
        try:
            self._refresh_labels()
        except tk.TclError:
            # The window can be torn down while callbacks are still queued;
            # a dying widget must not spam tracebacks during shutdown.
            self._alive = False
            return
        if self._alive:
            self.root.after(POLL_MS, self._tick)

    def _handle_message(self, kind, payload):
        if kind == "log":
            raw, level = payload
            self._raw_buffer.append(raw)
            if len(self._raw_buffer) > MAX_RAW_LINES:
                del self._raw_buffer[:len(self._raw_buffer) - MAX_RAW_LINES]
            if self._log_detailed and level != "STATUS":
                self._append_text(raw, "dim")
            elif level != "STATUS":
                human = humanize(raw)
                if human:
                    self._append_text(human, self._tag_for(level))
        elif kind == "status":
            self._on_status_line(payload)
        elif kind == "auth_done":
            ok, cost, detail, was_ok_already = payload
            if ok:
                self._last_recover_at = time.strftime("%H:%M:%S")
                self._last_recover_cost = cost
                if self._monitor and self._monitor.is_alive():
                    self._set_state(STATE_RUNNING)
                else:
                    self._set_state(STATE_IDLE)
            else:
                self._set_state(STATE_FAILED)
        elif kind == "monitor_done":
            if self._state != STATE_EXITING:
                self._set_state(STATE_IDLE)
                self._log("守护已停止。", "dim")
        elif kind == "show":
            self._show_window()
        elif kind == "exit":
            self._do_exit(payload)
        elif kind == "tray_exited":
            self._log("托盘已退出，程序即将关闭。", "dim")
            self._do_exit(None)

    def _queue_exit(self, reason):
        """可从任意线程调用 — 只入队。"""
        self._q.put(("exit", reason))

    def _on_status_line(self, status):
        """把守护循环的原始状态行翻成界面状态。"""
        m = re.search(r"next in (\d+)s", status)
        if m:
            self._last_check_at = time.strftime("%H:%M:%S")
        if "[DOWN]" in status:
            if self._outage_start is None:
                self._outage_start = time.time()
            self._set_state(STATE_AUTHING)
            m = re.search(r"auth #(\d+)", status)
            self._auth_attempt = int(m.group(1)) if m else 1
        elif "[OK]" in status:
            if self._outage_start is not None:
                cost = time.time() - self._outage_start
                self._last_recover_at = time.strftime("%H:%M:%S")
                self._last_recover_cost = cost
                self._recover_count += 1
                self._outage_start = None
            self._set_state(STATE_RUNNING)
        elif "[WARN]" in status:
            self._set_state(STATE_PENDING)

    @staticmethod
    def _tag_for(level):
        if level == "ERROR":
            return "error"
        if level in ("WARN", "DOWN"):
            return "warn"
        if level in ("RECOVER", "AUTH"):
            return "ok"
        return "dim"

    # ── 界面构建 ─────────────────────────────────────────────────────────
    def _build_ui(self):
        bg = "#f7f8fa"
        self.root.configure(bg=bg)

        header = tk.Frame(self.root, bg=bg)
        header.pack(fill="x", pady=(12, 2), padx=16)
        tk.Label(header, text=f"校园网自动认证 {core.VERSION}", bg=bg, fg="#2b2f36",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(header, text="断网自动重连", bg=bg, fg="#6b7280",
                 font=("Microsoft YaHei UI", 9)).pack(side="right")

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10), padding=(16, 7))
        style.configure("TFrame", background=bg)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(8, 12))

        self.tab_a = ttk.Frame(self.notebook)
        self.tab_b = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_a, text="  手动守护  ")
        self.notebook.add(self.tab_b, text="  每天自动守护  ")
        self.notebook.add(self.tab_settings, text="  设置  ")

        self._build_a_tab(self.tab_a)
        self._build_b_tab(self.tab_b)
        self._build_settings_tab(self.tab_settings)

        # 打开时定位到用户上次选的模式
        self.notebook.select(0 if self._config.get("mode", "silent") == "manual" else 1)

        self._append_text("已就绪。想要每天自动守护就切到「每天自动守护」页；"
                          "临时用一次就在本页点「开始守护」。", "dim")

    # ── 选项卡①：手动守护（模式 A）────────────────────────────────────────
    def _build_a_tab(self, parent):
        bg = "#f7f8fa"
        hero = tk.Frame(parent, bg=bg)
        hero.pack(fill="x", pady=(16, 2), padx=18)
        self.dot = tk.Canvas(hero, width=22, height=22, bg=bg, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 12))
        self._dot_id = self.dot.create_oval(2, 2, 20, 20,
                                            fill=STATE_COLOR[STATE_IDLE], outline="")
        self.state_label = tk.Label(hero, text=STATE_TEXT[STATE_IDLE], bg=bg,
                                    fg="#1b1f24", font=("Microsoft YaHei UI", 22, "bold"))
        self.state_label.pack(side="left")

        self.hint_label = tk.Label(parent, text="", bg=bg, fg="#3b4148",
                                   font=("Microsoft YaHei UI", 10), anchor="w")
        self.hint_label.pack(fill="x", pady=(0, 2), padx=18)
        self.meta_label = tk.Label(parent, text="", bg=bg, fg="#6b7280",
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self.meta_label.pack(fill="x", pady=(0, 12), padx=18)

        self.manual_box = tk.Frame(parent, bg=bg)
        self.manual_box.pack(fill="x", pady=(0, 10), padx=18)
        self.primary_btn = tk.Button(
            self.manual_box, text="▶  开始守护", font=("Microsoft YaHei UI", 13, "bold"),
            bg="#1f6feb", fg="white", activebackground="#1a5fd0",
            activeforeground="white", relief="flat", cursor="hand2",
            command=self._on_primary_click)
        self.primary_btn.pack(fill="x", ipady=11)
        sub = tk.Frame(self.manual_box, bg=bg)
        sub.pack(fill="x", pady=(8, 0))
        self.stop_btn = tk.Button(sub, text="停止守护", font=("Microsoft YaHei UI", 9),
                                  bg="#e3e6eb", fg="#2b2f36", relief="flat",
                                  cursor="hand2", command=self.stop_monitor)
        self.stop_btn.pack(side="left")
        self.auth_btn = tk.Button(sub, text="立即认证一次", font=("Microsoft YaHei UI", 9),
                                  bg="#e3e6eb", fg="#2b2f36", relief="flat",
                                  cursor="hand2", command=self.auth_now)
        self.auth_btn.pack(side="left", padx=(8, 0))

        # 运行日志：默认展开（用户要求常显）
        log_head = tk.Frame(parent, bg=bg)
        log_head.pack(fill="x", padx=18)
        self.log_toggle = tk.Button(log_head, text="▾ 运行日志",
                                    font=("Microsoft YaHei UI", 9), bg=bg,
                                    fg="#6b7280", relief="flat", cursor="hand2",
                                    activebackground=bg, command=self._toggle_log)
        self.log_toggle.pack(side="left")
        tk.Button(log_head, text="详细日志", font=("Microsoft YaHei UI", 9), bg=bg,
                  fg="#1f6feb", relief="flat", cursor="hand2", activebackground=bg,
                  command=self._toggle_detail).pack(side="left", padx=(10, 0))
        tk.Button(log_head, text="打开日志文件夹", font=("Microsoft YaHei UI", 9),
                  bg=bg, fg="#1f6feb", relief="flat", cursor="hand2",
                  activebackground=bg, command=self._open_log_dir).pack(side="right")

        self.log_frame = tk.Frame(parent, bg=bg)
        self.log_frame.pack(fill="both", expand=True, padx=18, pady=(4, 10))
        self.log_text = tk.Text(self.log_frame, height=8, wrap="word", bd=0,
                                bg="white", fg="#2b2f36", relief="flat",
                                font=("Microsoft YaHei UI", 9), padx=8, pady=6,
                                state="disabled")
        log_scroll = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("dim", "#59606a"), ("warn", "#b8730a"),
                           ("error", "#cc2b2b"), ("ok", "#1a9c4a")):
            self.log_text.tag_configure(tag, foreground=color)

    # ── 选项卡②：每天自动守护（模式 B）──────────────────────────────────
    def _build_b_tab(self, parent):
        bg = "#f7f8fa"
        tk.Label(parent, text="每天自动守护（平时完全不用管）", bg=bg, fg="#1b1f24",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(
            anchor="w", padx=18, pady=(20, 4))
        tk.Label(parent,
                 text="到点自己启动：全程无窗口、无托盘、不弹任何提示。\n"
                      "它安静地探测网络，断网立刻重连，跑满时长后自己退出，只在日志里留记录。",
                 bg=bg, fg="#59606a", justify="left",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=18, pady=(0, 18))

        card = tk.Frame(parent, bg="#eef0f4")
        card.pack(fill="x", padx=18, pady=(0, 12))
        row = tk.Frame(card, bg="#eef0f4")
        row.pack(anchor="w", padx=16, pady=(16, 8))
        tk.Label(row, text="每天", bg="#eef0f4", fg="#2b2f36",
                 font=("Microsoft YaHei UI", 11)).pack(side="left")
        self.silent_time_var = tk.StringVar(
            value=str(self._config.get("silent_start_time") or "23:00"))
        tk.Entry(row, textvariable=self.silent_time_var, width=7, relief="flat",
                 bg="white", justify="center",
                 font=("Microsoft YaHei UI", 12)).pack(side="left", padx=8, ipady=5)
        tk.Label(row, text="启动，守护", bg="#eef0f4", fg="#2b2f36",
                 font=("Microsoft YaHei UI", 11)).pack(side="left")
        self.silent_run_var = tk.StringVar(
            value=str(self._config.get("silent_run_minutes") or 30))
        tk.Entry(row, textvariable=self.silent_run_var, width=5, relief="flat",
                 bg="white", justify="center",
                 font=("Microsoft YaHei UI", 12)).pack(side="left", padx=8, ipady=5)
        tk.Label(row, text="分钟后自动停止", bg="#eef0f4", fg="#2b2f36",
                 font=("Microsoft YaHei UI", 11)).pack(side="left")

        self.deploy_btn = tk.Button(
            card, text="开启每天自动守护", font=("Microsoft YaHei UI", 13, "bold"),
            bg="#1f6feb", fg="white", activebackground="#1a5fd0",
            activeforeground="white", relief="flat", cursor="hand2",
            command=self._deploy_silent_task)
        self.deploy_btn.pack(fill="x", padx=16, pady=(4, 8), ipady=10)

        self.mode_note = tk.Label(card, text="", bg="#eef0f4", fg="#6b7280",
                                  font=("Microsoft YaHei UI", 9), justify="left",
                                  wraplength=580, anchor="w")
        self.mode_note.pack(fill="x", padx=16, pady=(0, 6))
        linkrow = tk.Frame(card, bg="#eef0f4")
        linkrow.pack(anchor="w", padx=16, pady=(0, 14))
        tk.Button(linkrow, text="测试一次静默运行", font=("Microsoft YaHei UI", 9),
                  bg="#eef0f4", fg="#1f6feb", relief="flat", cursor="hand2",
                  activebackground="#eef0f4",
                  command=self._test_silent_now).pack(side="left")
        tk.Button(linkrow, text="取消每天自动守护", font=("Microsoft YaHei UI", 9),
                  bg="#eef0f4", fg="#1f6feb", relief="flat", cursor="hand2",
                  activebackground="#eef0f4",
                  command=self._remove_silent_task).pack(side="left", padx=(14, 0))
        tk.Button(linkrow, text="打开日志文件夹", font=("Microsoft YaHei UI", 9),
                  bg="#eef0f4", fg="#1f6feb", relief="flat", cursor="hand2",
                  activebackground="#eef0f4",
                  command=self._open_log_dir).pack(side="left", padx=(14, 0))

    # ── 模式选择（放在「设置」页顶部，两个功能页也都可用）────────────────
    def _build_mode_chooser(self, parent):
        bg = "#f7f8fa"
        self.mode_var = tk.StringVar(value=self._config.get("mode", "silent"))
        box = tk.Frame(parent, bg=bg)
        box.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(box, text="默认打开哪个用法？（两个页面都在上面，随时可以切）",
                 bg=bg, fg="#2b2f36", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        radio = tk.Frame(box, bg=bg)
        radio.pack(anchor="w", pady=(4, 0))
        tk.Radiobutton(radio, text="每天自动守护（推荐）", variable=self.mode_var,
                       value="silent", bg=bg, fg="#1b1f24", activebackground=bg,
                       cursor="hand2", font=("Microsoft YaHei UI", 9),
                       command=self._on_mode_change).pack(side="left")
        tk.Radiobutton(radio, text="手动守护（窗口 + 托盘）", variable=self.mode_var,
                       value="manual", bg=bg, fg="#1b1f24", activebackground=bg,
                       cursor="hand2", font=("Microsoft YaHei UI", 9),
                       command=self._on_mode_change).pack(side="left", padx=(16, 0))
        self.close_tray_var = tk.BooleanVar(
            value=bool(self._config.get("close_to_tray", True)))
        tk.Checkbutton(box, text="手动守护时：关窗口缩到托盘（取消勾选则关窗口就退出）",
                       variable=self.close_tray_var, bg=bg, fg="#59606a",
                       activebackground=bg, cursor="hand2",
                       font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
        self.mode_note2 = tk.Label(box, text="", bg=bg, fg="#1a9c4a",
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self.mode_note2.pack(anchor="w", pady=(2, 0))

    def _on_mode_change(self):
        """用户切换默认模式时存进配置，并把界面切到对应选项卡。"""
        mode = self.mode_var.get()
        if self._write_config():
            try:
                self.notebook.select(0 if mode == "manual" else 1)
            except Exception:
                pass
            self.mode_note2.configure(
                text="已切到「手动守护」——点开始守护即可。" if mode == "manual"
                else "已切到「每天自动守护」——填好时间点开启即可。")

    # ── 设置页 ───────────────────────────────────────────────────────────
    def _build_settings_tab(self, parent):
        bg = "#f7f8fa"
        self._build_mode_chooser(parent)
        self.vars = {}
        rows = [
            ("username", "学号 / 账号", False),
            ("password", "密码", True),
            ("portal_url", "校园网认证地址", False),
            ("check_url", "断网检测地址", False),
        ]
        body = tk.Frame(parent, bg=bg)
        body.pack(fill="x", padx=18, pady=(18, 6))
        for i, (key, label, secret) in enumerate(rows):
            tk.Label(body, text=label, bg=bg, fg="#2b2f36", anchor="w",
                     font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, sticky="w", pady=4, padx=(0, 10))
            var = tk.StringVar(value=str(self._config.get(key, "") or ""))
            entry = tk.Entry(body, textvariable=var, font=("Microsoft YaHei UI", 9),
                             relief="flat", bg="white")
            if secret:
                entry.configure(show="●")
            entry.grid(row=i, column=1, sticky="ew", pady=4, ipady=4)
            self.vars[key] = var

        tk.Label(body, text="认证方式", bg=bg, fg="#2b2f36", anchor="w",
                 font=("Microsoft YaHei UI", 9)).grid(
            row=len(rows), column=0, sticky="w", pady=4, padx=(0, 10))
        self.method_var = tk.StringVar(value=self._config.get("auth_method", "portal_post"))
        ttk.Combobox(body, textvariable=self.method_var, state="readonly",
                     values=("portal_post", "http", "browser")).grid(
            row=len(rows), column=1, sticky="ew", pady=4)
        body.columnconfigure(1, weight=1)

        self.close_tray_var = self.close_tray_var  # created on the 使用模式 tab
        tk.Label(parent,
                 text="配置就存在程序旁边的 auto_login_config.json 里，双击可手动改；"
                      "程序第一次运行会自动生成它。",
                 bg=bg, fg="#6b7280", justify="left", wraplength=600,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=18, pady=(14, 4))

        action_row = tk.Frame(parent, bg=bg)
        action_row.pack(fill="x", padx=18, pady=(8, 12))
        tk.Button(action_row, text="保存设置", font=("Microsoft YaHei UI", 9, "bold"),
                  bg="#1f6feb", fg="white", relief="flat", cursor="hand2",
                  activebackground="#1a5fd0", activeforeground="white",
                  command=self._save_settings).pack(side="left")
        tk.Button(action_row, text="打开配置文件位置", font=("Microsoft YaHei UI", 9),
                  bg="#e3e6eb", fg="#2b2f36", relief="flat", cursor="hand2",
                  command=self._open_config_location).pack(side="left", padx=(8, 0))
        self.settings_note = tk.Label(action_row, text="", bg=bg, fg="#1a9c4a",
                                      font=("Microsoft YaHei UI", 9))
        self.settings_note.pack(side="left", padx=(10, 0))

    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width() or 700
        h = self.root.winfo_height() or 620
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 4)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ── 界面动作 ─────────────────────────────────────────────────────────
    def _on_primary_click(self):
        if self._monitor and self._monitor.is_alive():
            self.stop_monitor()
        else:
            self.start_monitor()

    def _toggle_log(self):
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
            self.log_toggle.configure(text="▸ 运行日志")
        else:
            self.log_frame.pack(fill="both", expand=True, padx=18, pady=(4, 8))
            self.log_toggle.configure(text="▾ 运行日志")

    def _toggle_detail(self):
        self._log_detailed = not self._log_detailed
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        if self._log_detailed:
            self._append_text("（已切换到详细日志，下面显示底层原始记录）", "dim")
            for raw in self._raw_buffer[-MAX_RAW_LINES:]:
                self._append_text(raw, "dim")
        else:
            self._append_text("（已切换回人话日志）", "dim")

    def _sync_settings_from_config(self):
        for key, var in self.vars.items():
            var.set(str(self._config.get(key, "") or ""))
        self.method_var.set(self._config.get("auth_method", "portal_post"))
        self.close_tray_var.set(bool(self._config.get("close_to_tray", True)))
        self.mode_var.set(self._config.get("mode", "manual"))
        self.silent_time_var.set(str(self._config.get("silent_start_time") or "23:00"))
        self.silent_run_var.set(str(self._config.get("silent_run_minutes") or 30))

    def _write_config(self):
        """把界面上所有可编辑项写回配置文件（原子写）。"""
        new = dict(self._config)
        for key, var in self.vars.items():
            new[key] = var.get().strip()
        new["auth_method"] = self.method_var.get()
        new["close_to_tray"] = bool(self.close_tray_var.get())
        new["mode"] = self.mode_var.get()
        new["silent_start_time"] = self.silent_time_var.get().strip()
        try:
            new["silent_run_minutes"] = int(self.silent_run_var.get().strip() or 30)
        except ValueError:
            messagebox.showerror("填错了", "运行时长要填数字（分钟）。")
            return None
        if new.get("silent_start_time") and not core._parse_hhmm(new["silent_start_time"]):
            messagebox.showerror("填错了", "每天启动时刻要写成 HH:MM，例如 23:00。")
            return None
        target = core.config_write_path()
        if not core.save_config(new, path=target):
            messagebox.showerror("保存失败", f"写不进配置：\n{target}\n请检查目录权限。")
            return None
        self._config = new
        return target

    def _save_settings(self):
        target = self._write_config()
        if not target:
            return
        self._refresh_labels()          # 刚填好账号，按钮要立刻从灰变可点
        if self._monitor and self._monitor.is_alive():
            self._log("设置已保存。守护中改的账号密码，下次启动守护时生效。", "ok")
        elif self._missing_credentials():
            self._log("设置已保存，但账号密码还没填完整。", "warn")
        else:
            self._log("设置已保存 ✓ 现在可以去主页或「每天自动守护」页启动了。", "ok")
        self.settings_note.configure(text="已保存 ✓")
        self.root.after(2500, lambda: self.settings_note.configure(text=""))

    def _open_config_location(self):
        target = core.ensure_config_file() or core.config_write_path()
        try:
            if os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.startfile(os.path.dirname(target))
        except Exception as e:
            messagebox.showwarning("打不开", f"无法打开配置位置：{e}")

    def _open_log_dir(self):
        try:
            log_dir = os.path.join(core.get_runtime_dir(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            os.startfile(log_dir)
        except Exception as e:
            messagebox.showwarning("打不开", f"无法打开日志文件夹：{e}")

    # ── 模式 B：静默守护的计划任务部署 ───────────────────────────────────
    def _is_admin(self):
        """当前进程是否已提权。决定要不要先跟用户解释 UAC 弹窗。"""
        if not sys.platform.startswith("win"):
            return True
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return True          # 判断不了就别拦着用户

    def _task_exists(self):
        """直接问计划任务服务，比信任子进程退出码可靠。

        必须和注册走同一条路（PowerShell cmdlet）：schtasks.exe 在这台中文
        机器上会把任务名里的字符搞坏、查询恒为空，于是「创建成功」被判成失败、
        「删除」被当成没有任务而跳过。
        """
        inner = (f"if (Get-ScheduledTask -TaskName '{SILENT_TASK_NAME}' "
                 f"-ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 3 }}")
        encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded],
                capture_output=True, creationflags=0x08000000)
            return proc.returncode == 0
        except Exception:
            return False

    def _self_elevate(self, reason):
        """用管理员身份重新打开本程序（弹一次 UAC，之后所有操作都不再需要）。"""
        try:
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, ""
            else:
                exe = sys.executable
                params = f'"{os.path.abspath(__file__)}"'
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, params, core.exe_dir(), 1)
            if rc <= 32:
                messagebox.showwarning(
                    "没能提权",
                    f"{reason}\n\n提权启动失败（错误码 {rc}）。\n"
                    "可以右键 CampusNet.exe →「以管理员身份运行」再试。")
                return False
            self._do_exit(None)
            return True
        except Exception as e:
            messagebox.showerror("提权失败", f"{reason}\n\n{e}")
            return False

    def _explain_elevation(self):
        """不是管理员时，在弹 UAC 之前先把话说清楚。返回 False 表示用户放弃。"""
        detail = (
            "「开启每天自动守护」需要注册一个 Windows 计划任务，"
            "这一步必须有管理员权限。\n\n"
            "接下来会弹出系统权限窗口，请点「是」——\n"
            "那一瞬间屏幕变暗、其它窗口不能点是正常的。\n\n"
            "你不需要为此单独用管理员身份打开本程序，点「是」就够了。"
        )
        if not self._is_admin():
            return messagebox.askokcancel(
                "即将请求管理员权限", detail + "\n\n现在继续吗？")
        return messagebox.askokcancel(
            "创建每天自动守护",
            f"将在 {self._config.get('silent_start_time')} 创建每日静默守护任务，"
            f"运行 {self._config.get('silent_run_minutes')} 分钟。\n\n继续吗？")

    def _run_elevated(self, command):
        """用 UAC 提权跑一条命令，返回 (退出码, 命令, 输出)。

        输出必须重定向进文件再读回来：包装用的 cmd 带 CREATE_NO_WINDOW，
        管道抓不到子进程的输出，而「脚本到底报了什么错」恰恰是排查失败时
        唯一有用的信息。用户取消 UAC 时 code = 1223。
        """
        bat = os.path.join(core.get_runtime_dir(), "_campusnet_task.bat")
        out = os.path.join(core.get_runtime_dir(), "_campusnet_task.out")
        try:
            with open(bat, "w", encoding="gbk") as f:
                f.write("@echo off\r\n")
                f.write("chcp 65001 >nul\r\n")       # 输出按 UTF-8 落盘
                f.write(f'{command} > "{out}" 2>&1\r\n')
                f.write(f'echo EXITCODE=%ERRORLEVEL% >> "{out}"\r\n')
            subprocess.run(["cmd", "/c", bat], capture_output=True,
                           creationflags=0x08000000)
            if not os.path.exists(out):
                return -2, command, "(脚本没有任何输出)"
            with open(out, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            body, _, tail = raw.rpartition("EXITCODE=")
            try:
                code = int(tail.strip())
            except ValueError:
                code = -2
            return code, command, body.strip()
        except Exception as e:
            return -1, f"(无法执行: {e})", ""
        finally:
            for path in (bat, out):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    def _report_task_failure(self, reason, command, output=""):
        """失败时给一条能自己走通的路，而不是只说一句「失败了」。

        `command` is the encoded command line, which is unreadable by design —
        for manual recovery tell the user the parameters instead, and point at
        setup_task.ps1 only if their copy happens to have it.
        """
        start_at = self._config.get("silent_start_time")
        run_min = self._config.get("silent_run_minutes")
        script = os.path.join(core.exe_dir(), "setup_task.ps1")
        lines = [reason, ""]
        if output:
            lines += ["程序回报：", output.strip()[:600], ""]
        lines += [
            "你想手动解决的话，二选一：",
            "",
            "① 直接用管理员身份重开本程序（推荐）",
            "   右键 CampusNet.exe →「以管理员身份运行」，",
            "   再回到「每天自动守护」页点一次按钮；",
            "",
        ]
        if os.path.exists(script):
            lines += [
                "② 或者在管理员 PowerShell 里粘贴这行：",
                f'   & "{script}" -Silent -SilentAt {start_at} -RunMinutes {run_min}',
            ]
        else:
            lines += [
                "② 或者在管理员 PowerShell 里粘贴这几行：",
                f"   $exe = '{sys.executable}'",
                f"   $act = New-ScheduledTaskAction -Execute $exe "
                f"-Argument '--silent --run-minutes {run_min}' "
                f"-WorkingDirectory '{core.exe_dir()}'",
                f"   $trg = New-ScheduledTaskTrigger -Daily -At {start_at}",
                f"   Register-ScheduledTask -TaskName '{SILENT_TASK_NAME}' "
                f"-Action $act -Trigger $trg -Force",
            ]
        if messagebox.askyesno("没能创建每日任务", "\n".join(lines) + "\n\n要现在以管理员身份重新打开本程序吗？"):
            self._self_elevate("需要管理员权限才能创建每日任务。")

    def _task_register_command(self, start_at, run_min):
        """Build the elevated command that registers the daily task.

        The registration is generated **here**, not delegated to
        setup_task.ps1, so the exe works on its own: someone can be handed
        nothing but CampusNet.exe and still get the daily task. (It also removes
        a whole class of bug — the script had to be found next to the exe, and
        ran in whatever folder it was launched from, which was wrong if the user
        started the exe by its full path from elsewhere.)

        -EncodedCommand carries the script as base64 UTF-16LE: no quoting or
        code-page hazards at all, which matters because the exe path can contain
        spaces and Chinese characters.
        """
        limit = max(1, int(run_min)) + 10          # 运行时长 + 10 分钟余量
        # Running from source means sys.executable is python.exe, which needs the
        # script path as well — otherwise the task would launch a bare
        # interpreter and die instantly. The frozen exe is self-contained.
        args = f"--silent --run-minutes {int(run_min)}"
        if not getattr(sys, "frozen", False):
            args = f'"{os.path.abspath(__file__)}" {args}'
        inner = (
            f"Register-ScheduledTask -TaskName '{SILENT_TASK_NAME}' "
            f"-Action (New-ScheduledTaskAction -Execute '{sys.executable}' "
            f"-Argument '{args}' "
            f"-WorkingDirectory '{core.exe_dir()}') "
            f"-Trigger (New-ScheduledTaskTrigger -Daily -At {start_at}) "
            f"-Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
            f"-MultipleInstances IgnoreNew "
            f"-ExecutionTimeLimit (New-TimeSpan -Minutes {limit}) "
            f"-RestartCount 0) -Force | Out-Null; exit 0"
        )
        encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
        return ("powershell -NoProfile -ExecutionPolicy Bypass "
                f"-EncodedCommand {encoded}")

    # 示例配置里的占位符：照抄 example 就没改过的用户会留下这些值
    PLACEHOLDERS = {"你的学号", "你的密码", "your_username", "your_password"}

    def _filled(self, value):
        text = str(value or "").strip()
        return bool(text) and text not in self.PLACEHOLDERS

    def _missing_credentials(self):
        """返回一句说明，列出还缺哪些必填项；配置齐全时返回 None。

        这是防呆的核心：每日任务在凌晨静默运行，失败时**没有任何界面可提示**
        （这正是静默模式的意义），所以必须在"创建任务"这一步就拦住，
        否则用户会得到一个到点必然失败、且悄无声息的任务。

        不复用 core._need_setup()：它面向命令行向导，语义不同（例如它把
        "http 模式只需 portal_url" 之外的情况也算需要配置）。这里按 GUI 的
        实际用法定制，并额外把示例里的占位符当成"没填"。
        """
        cfg = self._config
        method = cfg.get("auth_method", "portal_post")
        missing = []
        if method == "portal_post":
            if not self._filled(cfg.get("username")):
                missing.append("学号 / 账号")
            if not self._filled(cfg.get("password")):
                missing.append("密码")
        else:
            if not self._filled(cfg.get("portal_url")):
                missing.append("校园网认证地址")
        if not missing:
            return None
        return "、".join(missing) + f"（认证方式：{method}）"

    def _require_credentials(self):
        """缺项时提示并跳到设置页。返回 True 表示可以继续。"""
        missing = self._missing_credentials()
        if missing is None:
            return True
        self._log(f"还差：{missing} —— 已切到「设置」页，填好保存后再来。", "error")
        try:
            self.notebook.select(self.tab_settings)
        except Exception:
            pass
        messagebox.showwarning(
            "先把账号密码填上",
            f"还缺：{missing}\n\n"
            "现在创建任务的话，它到点会启动、但因为没有账号密码而直接退出，"
            "而且不会有任何提示（静默模式不弹窗），你只会发现网络没恢复。\n\n"
            "已经帮你切到「设置」页，填好后点「保存设置」，再回来点这个按钮。")
        return False

    def _deploy_silent_task(self):
        if self.mode_var.get() != "silent":
            self.mode_var.set("silent")
        target = self._write_config()
        if not target:
            return
        if not self._require_credentials():
            self.mode_note.configure(text="配置不完整，任务没有创建。", fg="#cc2b2b")
            return
        start_at = self._config["silent_start_time"]
        run_min = self._config["silent_run_minutes"]
        if not self._explain_elevation():
            self.mode_note.configure(text="已取消，没有做任何改动。", fg="#6b7280")
            return

        cmd = self._task_register_command(start_at, run_min)
        self.mode_note.configure(text="正在请求管理员权限…请在弹出的系统窗口点「是」。",
                                 fg="#6b7280")
        self.root.update_idletasks()

        rc, ran_cmd, output = self._run_elevated(cmd)

        if rc == ERROR_CANCELLED:
            self.mode_note.configure(text="你取消了管理员权限请求，任务没有创建。", fg="#cc2b2b")
            self._report_task_failure(
                "你在系统权限窗口点了「否」，所以任务没有创建。", ran_cmd, output)
            return

        if self._task_exists():
            self.mode_note.configure(
                text=f"已创建每日任务 ✓　每天 {start_at} 静默守护 {run_min} 分钟，"
                     f"全程无窗口、无托盘、无提示，只在日志里留记录。", fg="#1a9c4a")
            self._log(f"已创建每日静默任务：{start_at} 起运行 {run_min} 分钟", "ok")
            messagebox.showinfo(
                "设置好了",
                f"每天 {start_at} 会自动开始静默守护，运行 {run_min} 分钟后自己结束。\n\n"
                "整个过程你不会看到任何窗口或图标。\n\n"
                "不需要了就在这一页点「取消每天自动守护」。")
            return

        self.mode_note.configure(text="任务没有创建成功。", fg="#cc2b2b")
        self._report_task_failure(
            f"任务没有创建成功（脚本退出码 {rc}）。", ran_cmd, output)

    def _remove_silent_task(self):
        if not self._task_exists():
            self.mode_note.configure(text="当前没有每日任务，不需要取消。", fg="#6b7280")
            return
        if not messagebox.askyesno("取消每天自动守护",
                                   "确定要删除每日静默守护任务吗？\n"
                                   "之后每天不会再自动启动。"):
            return
        inner = (f"Unregister-ScheduledTask -TaskName '{SILENT_TASK_NAME}' "
                 f"-Confirm:$false -ErrorAction SilentlyContinue; exit 0")
        encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
        rc, ran_cmd, output = self._run_elevated(
            "powershell -NoProfile -ExecutionPolicy Bypass "
            f"-EncodedCommand {encoded}")
        if not self._task_exists():
            self.mode_note.configure(text="已取消每日自动守护。", fg="#6b7280")
            self._log("已删除每日静默任务", "dim")
            return
        self.mode_note.configure(text="删除失败。", fg="#cc2b2b")
        self._report_task_failure(f"删除任务失败（退出码 {rc}）。", ran_cmd, output)

    def _test_silent_now(self):
        """不起窗口，直接跑一次静默守护，让用户确信它真的不显示任何东西。"""
        self._write_config()
        if not self._require_credentials():
            self.mode_note.configure(text="配置不完整，没有试跑。", fg="#cc2b2b")
            return
        exe = sys.executable
        try:
            if getattr(sys, "frozen", False):
                subprocess.Popen([exe, "--silent", "--now", "--run-minutes", "1"])
            else:
                subprocess.Popen([exe, os.path.abspath(__file__),
                                  "--silent", "--now", "--run-minutes", "1"])
        except Exception as e:
            messagebox.showerror("启动失败", f"无法启动静默测试：{e}")
            return
        self.mode_note.configure(
            text="已经悄悄启动了一次静默守护（1 分钟），你不会看到任何窗口或图标——"
                 "这正是模式 B 的效果。结束后可在日志里看到记录。", fg="#1a9c4a")

    # ── 状态与文案 ───────────────────────────────────────────────────────
    def _set_state(self, state):
        self._state = state
        self._refresh_labels()

    def _refresh_labels(self):
        self.dot.itemconfigure(self._dot_id,
                               fill=STATE_COLOR.get(self._state, "#8a8a8a"))
        self.state_label.configure(text=STATE_TEXT.get(self._state, "待命中"))

        # 防呆：缺账号密码时，两个"启动"按钮一律不可用。
        # 守护循环本身也有守卫（start_monitor / run_silent_mode 都会拒绝），
        # 这里只是让用户一眼看出"还没配好"，而不是点了才发现。
        missing = self._missing_credentials()

        running = bool(self._monitor and self._monitor.is_alive())
        # 主按钮
        if running:
            self.primary_btn.configure(text="■  停止守护", bg="#b8730a",
                                       activebackground="#9c6108", state="normal")
        elif missing:
            self.primary_btn.configure(text="请先在「设置」页填账号密码", bg="#c9ced6",
                                       activebackground="#c9ced6", state="disabled")
        else:
            self.primary_btn.configure(text="▶  开始守护", bg="#1f6feb",
                                       activebackground="#1a5fd0", state="normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.auth_btn.configure(
            state="disabled" if (missing or (self._auth_thread
                                             and self._auth_thread.is_alive()))
            else "normal")

        # 每天自动守护页的按钮
        if missing:
            self.deploy_btn.configure(text="请先填账号密码再开启", bg="#c9ced6",
                                      activebackground="#c9ced6", state="disabled")
        else:
            self.deploy_btn.configure(text="开启每天自动守护", bg="#1f6feb",
                                      activebackground="#1a5fd0", state="normal")

        # 副标题
        if self._state == STATE_PENDING:
            self.hint_label.configure(text="正在做第一次网络检查…")
        elif self._state == STATE_RUNNING:
            last = f"上次检测 {self._last_check_at}" if self._last_check_at else "刚刚启动"
            self.hint_label.configure(text=f"网络正常 · {last}")
        elif self._state == STATE_AUTHING:
            attempt = getattr(self, "_auth_attempt", 1)
            self.hint_label.configure(text=f"已断网，正在自动重连（第 {attempt} 次尝试）")
        elif self._state == STATE_FAILED:
            self.hint_label.configure(text="重连没成功，后台仍在自动重试")
        elif self._state == STATE_IDLE:
            self.hint_label.configure(text="尚未开始守护" if not running else "")

        # 元信息
        parts = []
        if running and self._session_start:
            parts.append(f"已守护 {core.format_duration(time.time() - self._session_start)}")
        else:
            parts.append("未在守护")
        if self._recover_count:
            cost = f"（{self._last_recover_cost:.0f} 秒）" if self._last_recover_cost else ""
            parts.append(f"重连 {self._recover_count} 次 · 最近 {self._last_recover_at}{cost}")
        else:
            parts.append("本次还没有重连过")
        self.meta_label.configure(text="　·　".join(parts))

        if self._tray:
            status = f"{STATE_TEXT.get(self._state, '待命中')}"
            if self._last_check_at:
                status += f" · {self._last_check_at}"
            self._tray.set_status(status)

    def _append_text(self, text, tag="dim"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n", tag)
        total = int(self.log_text.index("end-1c").split(".")[0])
        if total > MAX_HUMAN_LINES:
            self.log_text.delete("1.0", f"{total - MAX_HUMAN_LINES + 1}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _log(self, text, tag="dim"):
        self._append_text(text, tag)

    # ── 窗口显示/隐藏/退出 ───────────────────────────────────────────────
    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _hide_window(self):
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _on_close_request(self):
        if self._config.get("close_to_tray", True) and self._tray:
            self._hide_window()
        else:
            self._do_exit(None)

    def _do_exit(self, reason):
        if self._shutting_down:
            return
        if reason:
            self._log(f"退出原因：{reason}", "error")
        self._set_state(STATE_EXITING)
        # Stop the polling loop BEFORE destroying widgets: a pending after()
        # callback firing on a dead canvas raises TclError during shutdown.
        self._alive = False
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description=f"Campus Network Auto-Login {core.VERSION} — 图形界面")
    parser.add_argument("--silent", action="store_true",
                        help="场景 B：静默守护（无窗口无托盘），等价于 auto_login.py --silent")
    parser.add_argument("--run-minutes", type=int, default=None, metavar="N",
                        help="守护持续分钟数（0 = 一直守护）")
    parser.add_argument("--now", action="store_true",
                        help="配合 --silent：立刻开始探测，不等静默启动时刻")
    parser.add_argument("--version", action="version", version=f"CampusNet {core.VERSION}")
    args = parser.parse_args()

    if args.silent:
        return core.run_silent_mode(args.run_minutes, interactive_launch=args.now)

    try:
        return GuiApp().run()
    except Exception as e:
        core.log(f"GUI failed to start: {e}", "ERROR")
        try:
            import tkinter as _tk
            root = _tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                f"图形界面启动失败：\n{e}\n\n日志位置：{os.path.join(core.get_runtime_dir(), "logs")}")
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
