"""Per-run measurements; resource sampling includes short lived compositor children."""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid


class RunMetrics:
    def __init__(self, outdir, data, stage):
        self.outdir, self.data = Path(outdir), data
        self.started = time.perf_counter()
        self.stop = threading.Event()
        self.finished = False
        self.data.update(run_id=uuid.uuid4().hex, stage=stage,
                         started_at=datetime.now(timezone.utc).isoformat(),
                         memory_sampling_interval_seconds=.25)
        try:
            import psutil
            self.process = psutil.Process()
        except ImportError:
            self.process = None
        self._sample()
        self.thread = threading.Thread(target=self._watch, name='resource-measurement', daemon=True)
        self.thread.start()
        atexit.register(self.finish)

    def _sample(self):
        if not self.process:
            self.data['memory_measurement_available'] = False
            return
        try:
            import psutil
            own = self.process.memory_info()
            total = own.rss
            for child in self.process.children(recursive=True):
                try: total += child.memory_info().rss
                except (psutil.AccessDenied, psutil.NoSuchProcess): pass
            self.data.update(memory_measurement_available=True,
                             peak_process_rss_bytes=max(self.data.get('peak_process_rss_bytes', 0), getattr(own, 'peak_wset', own.rss)),
                             peak_process_tree_rss_bytes=max(self.data.get('peak_process_tree_rss_bytes', 0), total))
        except Exception:
            # Resource inspection must never interrupt a paid translation.
            self.data['memory_sampling_partial'] = True

    def _watch(self):
        while not self.stop.wait(.25): self._sample()

    def finish(self, status='interrupted_or_failed'):
        if self.finished: return
        self.finished = True
        self.stop.set()
        self.thread.join(timeout=1)
        self._sample()
        self.data.update(run_status=status, elapsed_seconds=time.perf_counter()-self.started,
                         finished_at=datetime.now(timezone.utc).isoformat())
        target = self.outdir/'runs'
        target.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        (target/(self.data['run_id']+'.json')).write_text(text, encoding='utf-8')
        (self.outdir/'last_run_metrics.json').write_text(text, encoding='utf-8')

