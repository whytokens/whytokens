"""Execute the dashboard's real tool rendering functions with a minimal DOM."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / 'static/modules/app.js').read_text(encoding='utf-8')
FORMAT = (ROOT / 'static/modules/format.js').read_text(encoding='utf-8')


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class TestCodexToolUI(unittest.TestCase):
    def test_commands_namespaces_multiple_calls_and_escaping(self):
        tool_code = APP[APP.index('function toolTruncate('):APP.index('function closeToolPanel(')]
        esc_code = FORMAT[FORMAT.index('export function esc('):FORMAT.index('/**', FORMAT.index('export function esc('))].replace('export ', '')
        harness = r"""
const elements = {};
const document = {
  createElement() { return {textContent: '', get innerHTML() {
    return this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }}; },
  getElementById(id) { return elements[id] ||= {innerHTML: '', textContent: '', classList: {remove() {}}}; }
};
let selectedSessionDetail = {turns: [{model: 'gpt-5.5', timestamp: 't1', tool_calls: [
  {name: 'functions.exec', input: {command: '<script>danger</script>'}},
  {name: 'clock.sleep', input: {duration_ms: 45000}}
]}]};
""" + esc_code + tool_code + r"""
const assert = require('node:assert/strict');
assert.equal(toolCallSummary({name:'exec', input:{command:'git status'}}), 'exec: git status');
assert.equal(toolCallSummary({name:'functions.exec', input:{command:'git status'}}), 'functions.exec: git status');
assert.equal(toolCallSummary({name:'clock.sleep', input:{duration_ms:45000}}), 'clock.sleep');
const cell = toolCellHTML(selectedSessionDetail.turns[0], 0);
assert.ok(cell.includes('+1'));
assert.ok(cell.includes('&lt;script&gt;'));
assert.ok(!cell.includes('<script>'));
openToolPanel(0);
assert.equal(elements['tool-panel-title'].textContent, '2 tool calls');
const body = elements['tool-panel-body'].innerHTML;
assert.ok(body.includes('functions.exec'));
assert.ok(body.includes('clock.sleep'));
assert.ok(body.includes('45000'));
assert.ok(body.includes('&lt;script&gt;'));
assert.ok(!body.includes('<script>'));
"""
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
