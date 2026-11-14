#!/usr/bin/env python3
"""Run within the same authorized stable Python interpreter as SMS monitoring."""
import runpy,sys
from pathlib import Path
worker=Path(__file__).with_name('workflow_worker.py')
sys.argv=[str(worker),'--max-cases','3']
runpy.run_path(str(worker),run_name='__main__')
