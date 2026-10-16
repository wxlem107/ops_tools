#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.probe —— 主机信息与性能指标采集（零依赖）。

优先直接读 /proc 与 /sys（Linux/麒麟，开销极小、无需装任何东西），
在非 Linux 上退化为调用系统 ps / df / netstat 等命令，取不到的项目留空而不报错。

提供：
  * sys_info()        —— 资产台账用：主机名、OS、内核、CPU、内存、磁盘、序列号、虚拟化…
  * metrics()         —— 监控看板用：CPU/内存/负载/磁盘/网络实时指标（带滑动窗口速率）
  * top_processes()   —— TOP 进程（CPU / 内存，支持两次采样算瞬时占用）
  * listen_ports()    —— 监听端口与关联进程
  * disk_io()         —— 磁盘 IO 累计与速率
"""
from __future__ import print_function

import os
import platform
import re
import socket
import time

from . import compat

IS_LINUX = platform.system().lower() == 'linux'
_SAMPLE = {}
_LOCK = compat.threading.Lock()
_CLK_TCK = 100.0
try:
    _CLK_TCK = float(os.sysconf('SC_CLK_TCK'))
except Exception:
    pass
_PAGE_SIZE = 4096
try:
    _PAGE_SIZE = os.sysconf('SC_PAGE_SIZE')
except Exception:
    pass


# ---------------------------------------------------------------- 基础读取
def _read(path, default=''):
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return default


def _read_lines(path):
    text = _read(path)
    return text.splitlines() if text else []


def _read_int(path, default=0):
    try:
        return int(_read(path, '0').strip())
    except Exception:
        return default


def _meminfo():
    out = {}
    if not IS_LINUX and compat.IS_WIN:
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong),
                            ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong),
                            ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong),
                            ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            out['MemTotal'] = int(st.ullTotalPhys)
            out['MemAvailable'] = int(st.ullAvailPhys)
            out['SwapTotal'] = int(st.ullTotalPageFile)
            out['SwapFree'] = int(st.ullAvailPageFile)
        except Exception:
            pass
        return out
    for line in _read_lines('/proc/meminfo'):
        parts = line.split(':')
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        val = parts[1].strip().split()
        try:
            out[key] = int(val[0]) * 1024 if len(val) > 1 and val[1] == 'kB' else int(val[0])
        except (ValueError, IndexError):
            continue
    return out


def _windows_cpu_times():
    """Windows 下通过 GetSystemTimes 取 CPU 累计时间（空闲/内核/用户，单位 100ns）"""
    try:
        import ctypes

        class _FileTime(ctypes.Structure):
            _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]

        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                    ctypes.byref(user)):
            return None
        to64 = lambda ft: (ft.high << 32) | ft.low            # noqa: E731
        k = to64(kernel)
        u = to64(user)
        return {'idle': to64(idle), 'total': k + u}
    except Exception:
        return None


# ---------------------------------------------------------------- 资产信息
def _os_release():
    info = {}
    for line in _read_lines('/etc/os-release'):
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        info[key.strip()] = value.strip().strip('"')
    if not info:
        for path in ('/etc/redhat-release', '/etc/centos-release', '/etc/kylin-release',
                     '/etc/system-release', '/etc/issue'):
            text = _read(path).strip()
            if text:
                info['PRETTY_NAME'] = text.splitlines()[0]
                break
    return info


def _cpu_info():
    model = ''
    cores = 0
    phys = 0
    mhz = 0.0
    flags = set()
    for line in _read_lines('/proc/cpuinfo'):
        if ':' not in line:
            continue
        key, value = [p.strip() for p in line.split(':', 1)]
        if key in ('model name', 'Processor', 'cpu model', 'Hardware'):
            model = model or value
        elif key == 'processor':
            cores += 1
        elif key == 'physical id':
            phys = max(phys, compat.to_int(value, 0) + 1)
        elif key == 'cpu MHz':
            try:
                mhz = max(mhz, float(value))
            except ValueError:
                pass
        elif key in ('flags', 'Features'):
            flags.update(value.split())
    return {'model': model, 'cores': cores, 'sockets': phys or 1, 'mhz': round(mhz, 1),
            'virtual': ('hypervisor' in flags) or ('vmx' in flags and 'hypervisor' in flags)}


def _virt_type():
    for cmd in (['systemd-detect-virt'],):
        if compat.which(cmd[0]):
            r = compat.run_cmd(cmd, timeout=3)
            out = (r['stdout'] or '').strip()
            if out and out != 'none':
                return out
    return ''


def _serial():
    for path in ('/sys/class/dmi/id/product_serial', '/sys/class/dmi/id/board_serial'):
        text = _read(path).strip()
        if text and text.lower() not in ('none', 'to be filled by o.e.m.'):
            return text
    return ''


def _vendor_model():
    vendor = _read('/sys/class/dmi/id/sys_vendor').strip() or _read('/sys/class/dmi/id/board_vendor').strip()
    model = _read('/sys/class/dmi/id/product_name').strip()
    return vendor, model


def _ips():
    out = []
    try:
        if IS_LINUX and compat.which('ip'):
            r = compat.run_cmd(['ip', '-o', '-4', 'addr', 'show'], timeout=4)
            for line in (r['stdout'] or '').splitlines():
                m = re.search(r'\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', line)
                if m and m.group(1) != 'lo':
                    out.append({'iface': m.group(1), 'ip': m.group(2), 'prefix': m.group(3)})
        else:
            r = compat.run_cmd(['ifconfig'], timeout=4)
            for line in (r['stdout'] or '').splitlines():
                m = re.search(r'inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)', line)
                if m and not m.group(1).startswith('127.'):
                    out.append({'iface': '', 'ip': m.group(1), 'prefix': ''})
    except Exception:
        pass
    if not out:
        try:
            out.append({'iface': '', 'ip': socket.gethostbyname(socket.gethostname()), 'prefix': ''})
        except Exception:
            pass
    return out


def sys_info():
    """系统与硬件画像（用于资产台账自动采集）"""
    uname = platform.uname()
    mem = _meminfo()
    osrel = _os_release()
    cpu = _cpu_info()
    vendor, model = _vendor_model()
    boot = 0.0
    try:
        boot = time.time() - float(_read('/proc/uptime').split()[0])
    except Exception:
        pass
    info = {
        'hostname': socket.gethostname(),
        'fqdn': '',
        'os_name': osrel.get('PRETTY_NAME') or osrel.get('NAME') or platform.system(),
        'os_id': osrel.get('ID', ''),
        'os_version': osrel.get('VERSION_ID', ''),
        'kernel': uname.release,
        'arch': uname.machine,
        'cpu_model': cpu['model'],
        'cpu_cores': cpu['cores'] or os.cpu_count() if hasattr(os, 'cpu_count') else cpu['cores'],
        'cpu_sockets': cpu['sockets'],
        'cpu_mhz': cpu['mhz'],
        'mem_total': mem.get('MemTotal', 0),
        'swap_total': mem.get('SwapTotal', 0),
        'vendor': vendor,
        'product': model,
        'serial': _serial(),
        'virt': _virt_type(),
        'boot_time': boot,
        'uptime': time.time() - boot if boot else 0,
        'python': platform.python_version(),
        'java': '',
        'ips': _ips(),
        'timezone': time.strftime('%Z'),
        'collected_at': time.time(),
    }
    try:
        info['fqdn'] = socket.getfqdn()
    except Exception:
        pass
    java = compat.which('java')
    if java:
        r = compat.run_cmd([java, '-version'], timeout=6)
        out = (r['stdout'] or '') + (r['stderr'] or '')
        m = re.search(r'version "([^"]+)"', out)
        if m:
            info['java'] = m.group(1)
    return info


# ---------------------------------------------------------------- 实时指标
def _cpu_snapshot():
    text = _read('/proc/stat')
    for line in text.splitlines():
        if line.startswith('cpu '):
            fields = [compat.to_int(x, 0) for x in line.split()[1:]]
            while len(fields) < 8:
                fields.append(0)
            idle = fields[3] + fields[4]
            total = sum(fields[:8])
            return {'idle': idle, 'total': total}
    return {'idle': 0, 'total': 0}


def _net_snapshot():
    rx = tx = 0
    for line in _read_lines('/proc/net/dev'):
        if ':' not in line:
            continue
        iface, rest = line.split(':', 1)
        if iface.strip() in ('lo',):
            continue
        fields = rest.split()
        if len(fields) >= 9:
            rx += compat.to_int(fields[0], 0)
            tx += compat.to_int(fields[8], 0)
    return {'rx': rx, 'tx': tx}


def _disk_snapshot():
    """从 /proc/diskstats 累计读写下发字节（跳过 loop/ram 设备）"""
    rd = wr = 0
    for line in _read_lines('/proc/diskstats'):
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if name.startswith(('loop', 'ram', 'dm-', 'sr')) or re.search(r'\d+$', name):
            continue
        rd += compat.to_int(fields[5], 0) * 512
        wr += compat.to_int(fields[9], 0) * 512
    return {'read': rd, 'write': wr}


def _rate(name, current, elapsed, keys):
    """用上一份采样算速率（首个采样返回 0）"""
    prev = _SAMPLE.get(name)
    out = {}
    if prev:
        dt = max(elapsed, 0.001)
        for key in keys:
            delta = current.get(key, 0) - prev.get(key, 0)
            out[key + '_rate'] = max(0.0, delta / dt)
    else:
        for key in keys:
            out[key + '_rate'] = 0.0
    _SAMPLE[name] = current
    return out


def metrics():
    """一次完整的实时指标采样（带速率计算，需连续调用才准确）"""
    now = time.time()
    with _LOCK:
        last_ts = _SAMPLE.get('__ts', now)
        elapsed = now - last_ts
        _SAMPLE['__ts'] = now

        cpu_now = _cpu_snapshot()
        if cpu_now['total'] == 0 and compat.IS_WIN:
            win_times = _windows_cpu_times()
            if win_times:
                cpu_now = win_times
        cpu_prev = _SAMPLE.get('__cpu')
        cpu_percent = 0.0
        if cpu_prev and cpu_now['total'] > cpu_prev['total']:
            d_total = cpu_now['total'] - cpu_prev['total']
            d_idle = cpu_now['idle'] - cpu_prev['idle']
            cpu_percent = max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))
        _SAMPLE['__cpu'] = cpu_now

        net = _net_snapshot()
        net_rate = _rate('__net', net, elapsed, ('rx', 'tx'))
        disk = _disk_snapshot()
        disk_rate = _rate('__disk', disk, elapsed, ('read', 'write'))

    load = [0.0, 0.0, 0.0]
    try:
        load = [float(x) for x in _read('/proc/loadavg').split()[:3]]
    except Exception:
        try:
            load = list(os.getloadavg())
        except Exception:
            pass

    mem = _meminfo()
    mem_total = mem.get('MemTotal', 0)
    mem_avail = mem.get('MemAvailable', mem.get('MemFree', 0) + mem.get('Buffers', 0) + mem.get('Cached', 0))
    mem_used = max(0, mem_total - mem_avail)
    swap_total = mem.get('SwapTotal', 0)
    swap_free = mem.get('SwapFree', 0)

    disks = []
    for mount, total, used, free in _mounts():
        disks.append({'mount': mount, 'total': total, 'used': used, 'free': free,
                      'percent': round(100.0 * used / total, 1) if total else 0.0})

    uptime = 0.0
    try:
        uptime = float(_read('/proc/uptime').split()[0])
    except Exception:
        try:
            import ctypes
            uptime = float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0
        except Exception:
            pass

    procs = _proc_count()
    conns = _conn_count()
    return {
        'ts': now,
        'cpu_percent': round(cpu_percent, 1),
        'cpu_cores': os.cpu_count() if hasattr(os, 'cpu_count') else _cpu_info()['cores'],
        'load': load,
        'mem': {
            'total': mem_total, 'used': mem_used, 'free': mem_total - mem_used,
            'available': mem_avail,
            'percent': round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0,
            'cached': mem.get('Cached', 0), 'buffers': mem.get('Buffers', 0),
        },
        'swap': {
            'total': swap_total, 'used': max(0, swap_total - swap_free),
            'percent': round(100.0 * (swap_total - swap_free) / swap_total, 1) if swap_total else 0.0,
        },
        'disks': disks,
        'net': {'rx': net['rx'], 'tx': net['tx'],
                'rx_rate': round(net_rate['rx_rate'], 1), 'tx_rate': round(net_rate['tx_rate'], 1)},
        'disk_io': {'read': disk['read'], 'write': disk['write'],
                    'read_rate': round(disk_rate['read_rate'], 1),
                    'write_rate': round(disk_rate['write_rate'], 1)},
        'uptime': uptime,
        'processes': procs,
        'connections': conns,
        'users': _logged_users(),
    }


def _mounts():
    """列出本地文件系统挂载点与容量（过滤 tmpfs / 虚拟文件系统）"""
    out = []
    skip_fs = ('tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs', 'cgroup',
               'cgroup2', 'devpts', 'mqueue', 'debugfs', 'tracefs', 'securityfs',
               'pstore', 'bpf', 'autofs', 'fusectl', 'configfs', 'hugetlbfs', 'ramfs')
    seen = set()
    for line in _read_lines('/proc/mounts'):
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount, fstype = fields[0], fields[1], fields[2]
        if fstype in skip_fs or not device.startswith('/'):
            continue
        if mount in seen:
            continue
        seen.add(mount)
        total, used, free = compat.disk_usage(mount)
        if total:
            out.append((mount, total, used, free))
    if not out:
        for mount in ('/', 'C:\\' if compat.IS_WIN else '/'):
            total, used, free = compat.disk_usage(mount)
            if total:
                out.append((mount, total, used, free))
                break
    return out


def _proc_count():
    if compat.IS_WIN:
        return len(_process_list_tasklist())
    if not IS_LINUX:
        r = compat.run_cmd('ps -e 2>/dev/null | wc -l', timeout=4)
        return max(0, compat.to_int((r['stdout'] or '0').strip(), 0) - 1)
    try:
        return len([n for n in os.listdir('/proc') if n.isdigit()])
    except Exception:
        return 0


def _conn_count():
    total = 0
    for proto in ('tcp', 'tcp6', 'udp', 'udp6'):
        lines = _read_lines('/proc/net/' + proto)
        total += max(0, len(lines) - 1)
    return total


def _logged_users():
    """当前登录用户（utmp 为二进制格式，这里直接复用 who 命令）"""
    users = []
    r = compat.run_cmd('who 2>/dev/null', timeout=3)
    for line in (r['stdout'] or '').splitlines():
        if line.strip():
            users.append(line.strip())
    return users


# ---------------------------------------------------------------- 进程
def _read_proc(pid):
    """解析 /proc/<pid>，返回进程字典；失败返回 None"""
    base = '/proc/%s' % pid
    try:
        with open(base + '/stat', 'r') as f:
            stat = f.read()
    except Exception:
        return None
    # comm 可能带空格/括号，取最后一个 ) 之后的部分
    idx = stat.rfind(')')
    if idx < 0:
        return None
    comm = stat[stat.find('(') + 1:idx]
    fields = stat[idx + 2:].split()
    if len(fields) < 22:
        return None
    try:
        state = fields[0]
        utime = compat.to_int(fields[11], 0)
        stime = compat.to_int(fields[12], 0)
        threads = compat.to_int(fields[17], 0)
        starttime = compat.to_int(fields[19], 0)
        rss_pages = compat.to_int(fields[21], 0)
    except IndexError:
        return None
    try:
        with open(base + '/cmdline', 'rb') as f:
            cmdline = f.read(4096).replace(b'\x00', b' ').strip().decode('utf-8', 'replace')
    except Exception:
        cmdline = ''
    try:
        uid = os.stat(base).st_uid
    except Exception:
        uid = None
    return {
        'pid': compat.to_int(pid, 0),
        'name': comm,
        'cmdline': cmdline or comm,
        'state': state,
        'cpu_ticks': utime + stime,
        'rss': rss_pages * _PAGE_SIZE,
        'threads': threads,
        'starttime': starttime,
        'uid': uid,
    }


def process_list():
    if compat.IS_WIN:
        return _process_list_tasklist()
    if not IS_LINUX:
        return _process_list_ps()
    out = []
    try:
        pids = [n for n in os.listdir('/proc') if n.isdigit()]
    except Exception:
        return []
    for pid in pids:
        info = _read_proc(pid)
        if info:
            out.append(info)
    return out


def _process_list_tasklist():
    """Windows 兜底：解析 tasklist CSV 输出"""
    r = compat.run_cmd('tasklist /fo csv /nh', timeout=10)
    out = []
    import csv as _csv
    try:
        rows = list(_csv.reader((r['stdout'] or '').splitlines()))
    except Exception:
        return out
    for row in rows:
        if len(row) < 5:
            continue
        name = compat.safe_str(row[0]).strip('"')
        pid_text = compat.safe_str(row[1]).strip('"')
        mem_text = compat.safe_str(row[4]).strip('"').replace(',', '').replace(' K', '').strip()
        out.append({
            'pid': compat.to_int(pid_text, 0),
            'name': name,
            'cmdline': name,
            'state': 'S',
            'cpu_ticks': 0,
            'cpu_percent': 0.0,
            'rss': compat.to_int(mem_text, 0) * 1024,
            'threads': 0,
            'uid': None,
        })
    return out


def _process_list_ps():
    """类 Unix 兜底：用 ps 输出"""
    r = compat.run_cmd('ps -eo pid,pcpu,pmem,rss,stat,comm 2>/dev/null', timeout=6)
    out = []
    for line in (r['stdout'] or '').splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        out.append({'pid': compat.to_int(parts[0], 0), 'name': parts[5],
                    'cmdline': parts[5], 'state': parts[4],
                    'cpu_percent': compat.to_int(parts[1], 0),
                    'cpu_ticks': 0, 'rss': compat.to_int(parts[3], 0) * 1024,
                    'threads': 0, 'uid': None})
    return out


def top_processes(n=10, sort='cpu'):
    """TOP 进程；连续两次调用（间隔 >0.2s）可算出瞬时 CPU 占用"""
    procs = process_list()
    now = time.time()
    with _LOCK:
        prev = _SAMPLE.get('__procs')
        prev_ts = _SAMPLE.get('__procs_ts', now)
        dt = max(now - prev_ts, 0.001)
        _SAMPLE['__procs'] = {p['pid']: p['cpu_ticks'] for p in procs}
        _SAMPLE['__procs_ts'] = now
    for p in procs:
        if prev and p['pid'] in prev:
            delta = p['cpu_ticks'] - prev[p['pid']]
            p['cpu_percent'] = round(max(0.0, delta / _CLK_TCK / dt * 100.0), 1)
        else:
            p['cpu_percent'] = 0.0
    key = (lambda p: p.get('rss') or 0) if sort == 'mem' else (lambda p: p.get('cpu_percent') or 0)
    procs.sort(key=key, reverse=True)
    return procs[:n]


def find_process(keyword, limit=20):
    """按名称/命令行关键字找进程"""
    keyword = compat.safe_str(keyword).lower()
    out = []
    for p in process_list():
        if keyword in p['name'].lower() or keyword in p['cmdline'].lower():
            out.append(p)
            if len(out) >= limit:
                break
    return out


def kill_process(pid, force=False):
    """终止进程（先 TERM 后 KILL）；返回 (ok, message)"""
    pid = compat.to_int(pid, 0)
    if pid <= 1:
        return False, '非法 PID'
    try:
        sig = 9 if force else 15
        if compat.IS_WIN:
            compat.run_cmd(['taskkill', '/F', '/PID', str(pid)], timeout=8)
        else:
            os.kill(pid, sig)
        return True, '已向 PID %d 发送信号 %d' % (pid, sig)
    except Exception as e:                                    # noqa: BLE001
        return False, compat.safe_str(e)


# ---------------------------------------------------------------- 端口
def _hex_ip(raw, is_v6=False):
    """把 /proc/net/tcp 里的十六进制地址转成可读 IP"""
    try:
        if not is_v6:
            return '%d.%d.%d.%d' % (int(raw[6:8], 16), int(raw[4:6], 16),
                                    int(raw[2:4], 16), int(raw[0:2], 16))
        # v6：按 4 字节小端分组
        parts = [raw[i:i + 8] for i in range(0, 32, 8)]
        out = []
        for part in parts:
            b = [int(part[i:i + 2], 16) for i in range(0, 8, 2)]
            out.extend(reversed(b))
        hexstr = ''.join('%02x' % x for x in out)
        return ':'.join(hexstr[i:i + 4] for i in range(0, 32, 4)).lstrip(':') or '::'
    except Exception:
        return raw


def listen_ports():
    """监听中的 TCP 端口（含关联 PID）"""
    ports = []
    for proto in ('tcp', 'tcp6'):
        is_v6 = proto.endswith('6')
        lines = _read_lines('/proc/net/' + proto)
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            if fields[3] != '0A':                       # 0A = LISTEN
                continue
            local = fields[1]
            if ':' not in local:
                continue
            addr, port_hex = local.rsplit(':', 1)
            ports.append({
                'proto': proto.upper(),
                'addr': _hex_ip(addr, is_v6),
                'port': compat.to_int(port_hex, 0, 0, 65535),
                'inode': fields[9],
                'pid': None,
                'process': '',
            })
    if ports:
        _attach_pids(ports)
    ports.sort(key=lambda p: p['port'])
    return ports


def _attach_pids(ports):
    """通过 /proc/<pid>/fd 里的 socket inode 反查端口所属进程"""
    want = {}
    for p in ports:
        want.setdefault(p['inode'], []).append(p)
    if not want:
        return
    try:
        pids = [n for n in os.listdir('/proc') if n.isdigit()]
    except Exception:
        return
    for pid in pids:
        fd_dir = '/proc/%s/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except Exception:
                continue
            if not target.startswith('socket:['):
                continue
            inode = target[8:-1]
            for item in want.get(inode, []):
                if item['pid'] is None:
                    info = _read_proc(pid)
                    item['pid'] = compat.to_int(pid, 0)
                    item['process'] = info['name'] if info else ''
        if all(item['pid'] is not None for group in want.values() for item in group):
            break
    return


def port_of_pid(pid):
    """某进程监听/占用的端口"""
    pid = compat.safe_str(pid)
    inodes = set()
    fd_dir = '/proc/%s/fd' % pid
    try:
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except Exception:
                continue
            if target.startswith('socket:['):
                inodes.add(target[8:-1])
    except Exception:
        return []
    return [p for p in listen_ports() if p['inode'] in inodes]


def disk_io():
    """各设备累计 IO（含顶部设备排行）"""
    devices = []
    for line in _read_lines('/proc/diskstats'):
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if name.startswith(('loop', 'ram', 'sr')) or re.search(r'\d+$', name):
            continue
        devices.append({
            'device': name,
            'read_bytes': compat.to_int(fields[5], 0) * 512,
            'write_bytes': compat.to_int(fields[9], 0) * 512,
            'read_ios': compat.to_int(fields[3], 0),
            'write_ios': compat.to_int(fields[7], 0),
            'util_ms': compat.to_int(fields[12], 0),
        })
    return devices


# ---------------------------------------------------------------- 健康判定
def health_rules(result, threshold=None):
    """把一次 metrics() 结果转成「异常项」列表（供巡检 / 告警复用）"""
    th = {'cpu': 90, 'mem': 90, 'disk': 85, 'swap': 60, 'load_per_core': 2.0}
    th.update(threshold or {})
    issues = []
    if result.get('cpu_percent', 0) > th['cpu']:
        issues.append({'level': 'warn', 'item': 'CPU',
                       'message': 'CPU 使用率 %.1f%% 超过 %s%%' % (result['cpu_percent'], th['cpu'])})
    mem = result.get('mem') or {}
    if mem.get('percent', 0) > th['mem']:
        issues.append({'level': 'warn', 'item': '内存',
                       'message': '内存使用率 %.1f%% 超过 %s%%' % (mem['percent'], th['mem'])})
    swap = result.get('swap') or {}
    if swap.get('percent', 0) > th['swap']:
        issues.append({'level': 'warn', 'item': 'Swap',
                       'message': 'Swap 使用率 %.1f%% 超过 %s%%' % (swap['percent'], th['swap'])})
    for disk in result.get('disks') or []:
        if disk.get('percent', 0) > th['disk']:
            issues.append({'level': 'error', 'item': '磁盘 %s' % disk['mount'],
                           'message': '磁盘使用率 %.1f%% 超过 %s%%' % (disk['percent'], th['disk'])})
    cores = result.get('cpu_cores') or 1
    load1 = (result.get('load') or [0])[0]
    if cores and load1 > th['load_per_core'] * cores:
        issues.append({'level': 'warn', 'item': '负载',
                       'message': '1 分钟负载 %.2f 超过 %s×核数' % (load1, th['load_per_core'])})
    return issues
