"""Pure-function checks of the Setup_Link UI helpers in setup_lab.js, run through node (Req 13.10, 15.2-15.4, 15.9, 15.10)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
PRELUDE = f"""
const vm = require('vm'), fs = require('fs');
const ctx = vm.createContext({{ module: {{ exports: {{}} }}, console }});
vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND / 'charts.js'))}, 'utf8') + '\\n;this.Charts = Charts;', ctx);
vm.runInContext('const esc = Charts.esc; const errorsBox = (e) => String(e.message); let META = null;', ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND / 'hist_analysis.js'))}, 'utf8') + '\\n;this.H = HistAnalysis;', ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND / 'setup_lab.js'))}, 'utf8').replace(/^let pollTimer/m, 'var pollTimer') + '\\n;this.S = SetupLinkUI;', ctx);
const S = ctx.S;
"""
def run(body: str):
    r = subprocess.run(["node", "-e", PRELUDE + body], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)
def test_unavailable_metric_is_disabled_and_never_checked():
    h = run("console.log(JSON.stringify(S.setupLinkMetricsHtml([{metric:'lap_time',unit:'s',available:true},{metric:'top_speed',unit:'kph',available:false}],['lap_time','top_speed'])))")
    assert 'value="top_speed" disabled>' in h and 'value="lap_time" checked>' in h
def test_every_factor_without_value_or_statement_is_reported():
    out = run("console.log(JSON.stringify(S.setupLinkMissingFactors([{name:'a'},{name:'b'},{name:'c'},{name:'d'}],{a:{value:'1',verified:true},b:{value:'',verified:true},c:{value:'2',verified:null}})))")
    assert out == ["b", "c", "d"]
def test_success_links_to_analysis_and_badge_uses_external_chip():
    out = run("console.log(JSON.stringify([S.setupLinkSuccessHtml({measurements_created:3,experiment_id:'x',verified:true}), S.setupLinkBadge({provenance_status:'OPENF1 \\u2014 external provider'})]))")
    assert "3 measurements created" in out[0] and 'href="#/e/x/analysis"' in out[0]
    assert "chip external" in out[1]
