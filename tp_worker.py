import json
import os
import subprocess
import sys
from pathlib import Path

if __name__=='__main__':
    if os.name=='nt':sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    args=sys.argv[1:]
    if '--book' in args:
        book=Path(args[args.index('--book')+1])
        if (book/'manifest.json').exists() and not (book/'book.json').exists():
            raise SystemExit(subprocess.call([sys.executable,str(Path(__file__).with_name('legacy_tp_worker.py')),*args]))
    from textbook_pdf.cli import worker_main
    try:raise SystemExit(worker_main())
    except (ValueError,KeyError,FileNotFoundError) as exc:
        print(f'未提交：{exc}',file=sys.stderr);raise SystemExit(2)
