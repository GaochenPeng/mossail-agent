"""本机实验 launcher；以实际文件边界探针验收，不等同安全认证。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile

from .workspace import write_json


@dataclass
class ProcessResult:
    returncode: int
    timed_out: bool
    stdout: str
    stderr: str


@dataclass
class MacSandbox:
    read_roots: list[Path]
    write_root: Path
    python: Path
    network: bool = False

    def profile(self) -> str:
        if sys.platform!='darwin' or not Path('/usr/bin/sandbox-exec').exists():
            raise RuntimeError('本机 launcher 需要 macOS sandbox-exec；未配置其他 launcher，不降为无隔离执行')
        base = subprocess.check_output([str(self.python),'-c','import sys;print(sys.base_prefix)'],text=True).strip()
        roots = [Path(base),self.python.parent.parent,self.write_root,*self.read_roots]
        filters = ' '.join('(subpath '+json.dumps(str(p.resolve()))+')' for p in roots)
        # 系统库依赖保留；用户目录、外部卷和用户临时文件按明确目录放行。
        protected = '(require-any (subpath "/Users") (subpath "/Volumes") (subpath "/private/tmp") (subpath "/private/var/folders"))'
        return '\n'.join(['(version 1)','(allow default)',
            '(deny file-read-data (require-all '+protected+' (require-not (require-any '+filters+'))))',
            '(deny file-write* (require-not (require-any (subpath '+json.dumps(str(self.write_root.resolve()))+') (literal "/dev/null"))))',
            '' if self.network else '(deny network*)'])

    def environment(self, extra=None) -> dict:
        self.write_root.mkdir(parents=True,exist_ok=True)
        for folder in ['home','tmp']: (self.write_root/folder).mkdir(exist_ok=True)
        return {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8',
            'HOME':str(self.write_root/'home'),'TMPDIR':str(self.write_root/'tmp'),
            'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',**(extra or {})}

    def command(self, args: list[str]) -> list[str]:
        profile = self.write_root/'sandbox.sb'
        profile.parent.mkdir(parents=True,exist_ok=True)
        profile.write_text(self.profile())
        return ['/usr/bin/sandbox-exec','-f',str(profile),*args]

    def run(self, args: list[str], *, cwd: Path, timeout=90, extra_env=None) -> ProcessResult:
        env = self.environment(extra_env)
        with (self.write_root/'stdout.log').open('w') as out, (self.write_root/'stderr.log').open('w') as err:
            process = subprocess.Popen(self.command(args),cwd=cwd,env=env,stdout=out,stderr=err,start_new_session=True)
            timed_out = False
            try: process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()
        return ProcessResult(process.returncode,timed_out,(self.write_root/'stdout.log').read_text(),(self.write_root/'stderr.log').read_text())

    def probe_denied_reads(self, paths: list[Path]) -> dict:
        program = '''import json,sys
def readable(path):
    try:
        with open(path, 'rb') as handle: handle.read(1)
        return True
    except PermissionError: return False
print(json.dumps([readable(path) for path in sys.argv[1:]]))
'''
        result = subprocess.run(self.command([str(self.python), '-c', program, *map(str, paths)]),
            cwd=self.write_root, env=self.environment(), capture_output=True, text=True, timeout=10)
        observation = json.loads(result.stdout) if result.returncode == 0 else None
        proof = {'passed': observation == [False] * len(paths),
                 'credential_files_readable': observation, 'returncode': result.returncode,
                 'scope': '用服务相同隔离配置尝试读取选定凭证文件；不输出文件内容。'}
        write_json(self.write_root/'credential-isolation.json', proof)
        if not proof['passed']:
            raise RuntimeError('凭证文件访问边界未通过，未启动候选服务')
        return proof

    def probe(self) -> dict:
        self.write_root.mkdir(parents=True,exist_ok=True)
        private = self.write_root.parent/('outside-'+self.write_root.name+'.txt')
        private.write_text('synthetic isolation canary')
        allowed = self.write_root/'allowed.txt';allowed.write_text('allowed')
        readonly = self.write_root.parent/('_readonly_'+self.write_root.name)
        readonly.mkdir(exist_ok=True)
        sealed = readonly/'sealed.txt';sealed.write_text('synthetic readonly material')
        if readonly not in self.read_roots: self.read_roots.append(readonly)
        unrelated = tempfile.NamedTemporaryFile(prefix='rsi-canary-',delete=False)
        unrelated.write(b'synthetic unrelated canary');unrelated.close()
        listener = socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        program = '''import json
import socket
from pathlib import Path
def read(path):
    try: Path(path).read_text(); return True
    except PermissionError: return False
def write(path):
    try: Path(path).write_text('probe'); return True
    except PermissionError: return False
try:
    connection=socket.create_connection(('127.0.0.1',ARGS[5]),timeout=1)
    connection.close();network=True
except OSError: network=False
print(json.dumps({'allowed_read':read(ARGS[0]),'allowed_write':write(ARGS[1]),
    'outside_read':read(ARGS[2]),'outside_write':write(ARGS[2]),
    'readonly_read':read(ARGS[3]),'readonly_write':write(ARGS[3]),
    'unrelated_read':read(ARGS[4]),'unrelated_write':write(ARGS[4]),'network_connect':network}))
'''
        program = 'ARGS='+repr([str(allowed),str(self.write_root/'written.txt'),str(private),str(sealed),unrelated.name,listener.getsockname()[1]])+'\n'+program
        try:
            result = self.run([str(self.python),'-c',program],cwd=self.write_root)
            observation = json.loads(result.stdout) if result.returncode==0 else None
            expected = {'allowed_read':True,'allowed_write':True,'outside_read':False,'outside_write':False,
                'readonly_read':True,'readonly_write':False,'unrelated_read':False,'unrelated_write':False,'network_connect':self.network}
            proof = {'launcher':'macos-seatbelt','passed':observation==expected,'observation':observation,
                'returncode':result.returncode,'network_enabled':self.network,
                'scope':'输出目录读写；封存目录只读；相邻和无关临时 canary 内容读写被拒绝；网络连接按开关验证。不是恶意代码安全认证。'}
            write_json(self.write_root/'isolation.json',proof)
            if not proof['passed']:
                raise RuntimeError('隔离探针失败，未执行候选；日志见 '+str(self.write_root))
            return proof
        finally:
            private.unlink(missing_ok=True)
            Path(unrelated.name).unlink(missing_ok=True)
            listener.close()
