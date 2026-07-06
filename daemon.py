import os, sys, subprocess, time, signal

def main():
    # Double fork to fully daemonize
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    
    # Redirect stdio
    sys.stdout.flush()
    sys.stderr.flush()
    si = open(os.devnull, 'r')
    so = open('/tmp/bot_daemon.log', 'a')
    se = open('/tmp/bot_daemon.log', 'a')
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())
    
    os.chdir('/home/z/my-project/telegram-bot')
    
    while True:
        proc = subprocess.Popen(
            [sys.executable, '-u', 'bot.py'],
            stdout=so, stderr=se
        )
        proc.wait()
        with open('/tmp/bot_daemon.log', 'a') as f:
            f.write(f"\n[{time.strftime('%H:%M:%S')}] Bot exited with code {proc.returncode}, restarting in 3s...\n")
        time.sleep(3)

if __name__ == '__main__':
    main()
