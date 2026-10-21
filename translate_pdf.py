"""Compatibility entry point; the legacy engine remains explicitly available."""
import os
import subprocess
import sys
from pathlib import Path

if __name__=='__main__':
    if os.name=='nt':
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
        sys.stderr.reconfigure(encoding='utf-8',errors='replace')
    legacy_flags={'--legacy','--import-ccswitch','--test','--only-md','--only-bilingual','--model'}
    legacy_assemble=False
    if '--assemble-only' in sys.argv and '--stage' not in sys.argv:
        if '--out' in sys.argv:
            directory=Path(sys.argv[sys.argv.index('--out')+1])
            legacy_assemble=(directory/'manifest.json').exists() and not (directory/'book.json').exists()
        elif len(sys.argv)>1 and not sys.argv[1].startswith('--'):
            pdf=Path(sys.argv[1])
            directory=pdf.parent/('译文_'+pdf.stem)
            legacy_assemble=(directory/'manifest.json').exists()
    if legacy_flags.intersection(sys.argv[1:]) or legacy_assemble:
        command=[sys.executable,str(Path(__file__).with_name('legacy_translate_pdf.py'))]+[a for a in sys.argv[1:] if a!='--legacy']
        raise SystemExit(subprocess.call(command))
    from textbook_pdf.cli import main
    try:raise SystemExit(main())
    except (ValueError,FileNotFoundError,RuntimeError) as exc:
        print(f'未完成：{exc}',file=sys.stderr)
        raise SystemExit(2)
