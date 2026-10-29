/* The saved report and local interface render the same data. Only the local interface makes requests. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const embedded = document.getElementById('report-data');
  const offline = Boolean(embedded);
  const token = document.querySelector('meta[name="archive-token"]').content;
  const number = value => typeof value === 'number' ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : value === null || value === undefined ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const date = value => value === null || value === undefined ? '—' : new Date(value * 1000).toISOString().replace('T', ' ').slice(0, 19);
  const shortDate = value => date(value).slice(0, 10);
  const reasons = {equidistant_boundaries:'Equidistant destinations', outside_tolerance:'Outside shift tolerance', target_outside_range:'Destination outside selected dates', source_contains_weather:'Source contains weather data', multiple_sources:'Multiple source records', missing_target:'Missing destination', target_has_no_anchor:'Destination has no anchor value', unit_mismatch:'Unit systems differ or are invalid', interval_mismatch:'Archive intervals differ', non_numeric_value:'Non-numeric or infinite value', invalid_aggregate:'Aggregation exceeds the numeric range', existing_value:'Destination has a different value'};
  let report;
  let details;
  let plan = null;
  let previewPage = 0;
  let fieldRequest = 0;
  let busy = false;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function showError(error) {
    byId('error').textContent = error.message || String(error);
    byId('error').hidden = false;
    if (error.backup) renderBackup(error.backup);
  }
  function renderBackup(backup) {
    const output = byId('backup-result');
    output.replaceChildren(element('strong', 'Backup saved and verified'), element('div', backup.path),
      element('p', `${number(backup.bytes)} bytes. Kept after this session ends.`, 'help'),
      element('div', `SHA-256: ${backup.sha256}`, 'backup-checksum'));
    const link = element('a', 'Download backup');
    link.href = backup.download; link.download = backup.filename; output.append(link); output.hidden = false;
  }
  function clearError() { byId('error').hidden = true; }
  async function request(path, options = {}) {
    const response = await fetch(path, {...options, headers:{'X-Archive-Token':token, 'Content-Type':'application/json', ...(options.headers || {})}});
    if (!response.ok) {
      const result = await response.json();
      const error = new Error(result.error || `Request failed (${response.status}).`);
      error.backup = result.backup;
      throw error;
    }
    return response;
  }
  function selectedFields() {
    return Array.from(document.querySelectorAll('.transfer-field:checked')).map(input => input.value);
  }
  function updateExportButton() {
    byId('export-copy').disabled = busy || !plan || !plan.totals.ready ||
      Boolean(plan.totals.blocked && !byId('ready-only').checked);
  }
  function updateFieldControls() {
    const anchor = byId('anchor').value;
    document.querySelectorAll('.transfer-field').forEach(input => {
      input.disabled = busy || input.value === anchor;
      if (input.value === anchor) input.checked = false;
    });
    document.querySelectorAll('.field-reducer').forEach(select => {
      select.disabled = busy || select.dataset.field === anchor;
      select.title = select.dataset.field === anchor ? 'The weather field identifies destination rows and cannot be transferred.' : '';
    });
  }
  function setBusy(value) {
    busy = value;
    document.querySelectorAll('#merge-form input, #merge-form select, #run-plan').forEach(control => {control.disabled = value;});
    updateFieldControls();
    byId('ready-only').disabled = value;
    byId('create-backup').disabled = value;
    updateExportButton();
  }
  function invalidatePlan() {
    if (offline) return;
    if (plan) byId('status').textContent = 'Settings changed. Run the dry run again.';
    plan = null;
    byId('plan-result').hidden = true;
    byId('export-result').hidden = true;
    updateFieldControls();
    byId('selected-count').textContent = `${selectedFields().length} selected`;
    updateExportButton();
  }
  function renderAlignment(data) {
    const count = data.offsets.filter(item => item.seconds !== 0).reduce((sum, item) => sum + item.count, 0);
    byId('alignment').classList.toggle('has-offsets', Boolean(count));
    byId('alignment-summary').textContent = !data.interval_seconds ? 'No usable archive interval. Alignment cannot be checked.' : count ? `${number(count)} of ${number(data.records)} rows fall between the ${number(data.interval_seconds)}-second archive boundaries.` : `All ${number(data.records)} rows are on the ${number(data.interval_seconds)}-second grid.`;
    const counts = byId('alignment-field-counts'); counts.replaceChildren();
    data.fields.filter(field => field.off_grid || (count && field.name === data.interval_basis)).forEach(field => {
      if (field.off_grid === undefined || field.off_grid === null) return;
      const item = element('span', `${field.name}: ${number(field.off_grid)} / ${number(field.count)} off grid`, field.off_grid ? 'offset-count' : 'aligned-count');
      counts.append(item);
    });
    const rows = byId('alignment-rows'); rows.replaceChildren();
    (data.alignment_examples || []).forEach(entry => {
      const row = element('tr'); row.classList.toggle('off-grid-row', entry.offset_seconds !== 0);
      row.append(element('td', date(entry.timestamp)), element('td', entry.offset_seconds ? `+${entry.offset_seconds}s` : 'On grid'));
      const values = element('td');
      const stored = Object.entries(entry.values);
      stored.slice(0, 4).forEach(([field,value]) => values.append(element('span', `${field}: ${number(value)}`, 'stored-value')));
      if (stored.length > 4) {
        const more = element('details'); more.append(element('summary', `${stored.length - 4} more fields`));
        stored.slice(4).forEach(([field,value]) => more.append(element('span', `${field}: ${number(value)}`, 'stored-value')));
        values.append(more);
      }
      row.append(values); rows.append(row);
    });
    byId('alignment-examples').hidden = !rows.children.length;
    byId('alignment-dry-run').hidden = offline || !count;
  }
  function renderArchive(data) {
    report = data;
    byId('source-name').textContent = data.source;
    byId('source-table').textContent = `Table ${data.table} · ${data.daily_tables.length} daily summary tables`;
    byId('record-count').textContent = number(data.records);
    byId('field-count').textContent = number(data.fields.length);
    const range = byId('date-range'); range.replaceChildren();
    if (data.records) range.append(element('span', shortDate(data.first_timestamp)), document.createTextNode(' to '), element('span', `${shortDate(data.last_timestamp)} UTC`));
    else range.textContent = 'No records in the selected range';
    byId('interval-value').textContent = data.interval_seconds ? `${number(data.interval_seconds)} s` : 'Unknown';
    byId('interval-note').textContent = data.interval_basis ? `Based on ${data.interval_basis}` : `${data.intervals.length} recorded interval value${data.intervals.length === 1 ? '' : 's'}`;
    byId('off-grid-count').textContent = data.interval_seconds ? number(data.offsets.filter(item => item.seconds !== 0).reduce((sum, item) => sum + item.count, 0)) : '—';
    byId('gap-count').textContent = number(data.gap_count);
    byId('short-count').textContent = number(data.short_interval_count);
    byId('unit-systems').textContent = data.units.map(item => `${({1:'US',16:'METRIC',17:'METRICWX'})[item.value] || number(item.value)} (${number(item.count)})`).join(', ') || 'None';
    byId('integrity-value').textContent = data.integrity.join(', ');
    byId('timezone-note').textContent = `Daily summaries: ${data.summary_timezone}. Timestamps: UTC.`;
    byId('version').textContent = data.version;
    renderAlignment(data);
    if (data.start !== null) byId('plan-start').value = new Date(data.start * 1000).toISOString().slice(0,19);
    if (data.stop !== null) byId('plan-stop').value = new Date(data.stop * 1000).toISOString().slice(0,19);
    if (data.interval_seconds) byId('merge-interval').value = data.interval_seconds;
    const chart = byId('chart-field');
    const anchor = byId('anchor');
    [chart,anchor].forEach(select => {
      const placeholder = element('option', 'Choose an observation…'); placeholder.value = '';
      select.replaceChildren(placeholder);
    });
    const fieldRows = byId('field-rows'); fieldRows.replaceChildren();
    data.fields.forEach(field => {
      chart.append(element('option', field.name)); chart.lastChild.value = field.name;
      anchor.append(element('option', field.name)); anchor.lastChild.value = field.name;
      const row = element('tr');
      const choice = element('td');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.value = field.name; checkbox.className = 'transfer-field'; checkbox.setAttribute('aria-label', `Transfer ${field.name}`);
      checkbox.addEventListener('change', invalidatePlan); choice.append(checkbox); row.append(choice);
      row.append(element('td', field.name, 'field-name'), element('td', number(field.count)), element('td', field.off_grid === undefined || field.off_grid === null ? '—' : number(field.off_grid), field.off_grid ? 'offset-count' : ''), element('td', number(field.missing)));
      const availability = element('td'); const track = element('span', undefined, 'coverage-inline'); const fill = element('i'); fill.style.width = `${field.coverage * 100}%`; track.append(fill); availability.append(track, document.createTextNode(`${(field.coverage * 100).toFixed(1)}%`)); row.append(availability, element('td', field.type || 'Unspecified', 'storage-type'));
      const methodCell = element('td'); const method = element('select'); method.className = 'field-reducer'; method.dataset.field = field.name; method.setAttribute('aria-label', `Aggregation method for ${field.name}`);
      const placeholder = element('option', 'Choose a method…'); placeholder.value = ''; method.append(placeholder);
      [['mean','Mean'],['last','Last value'],['first','First value'],['sum','Sum'],['min','Minimum'],['max','Maximum']].forEach(([value,label]) => {const option = element('option',label); option.value = value; method.append(option);});
      method.value = ''; method.addEventListener('change', () => {if (method.value) checkbox.checked = true; invalidatePlan();}); methodCell.append(method); row.append(methodCell); fieldRows.append(row);
    });
    anchor.value = ''; chart.value = '';
    renderField(null);
    const maximum = Math.max(1, ...data.offsets.map(item => item.count));
    const offsetBars = byId('offset-bars'); offsetBars.replaceChildren();
    data.offsets.slice(0, 8).forEach(item => {
      const row = element('div', undefined, item.seconds ? 'offset-row off-grid-offset' : 'offset-row'); const bar = element('div'); const fill = element('i'); fill.style.width = `${item.count / maximum * 100}%`; bar.append(fill); row.append(element('span', item.seconds ? `+${item.seconds}s` : 'On grid'), bar, element('span', number(item.count))); offsetBars.append(row);
    });
    if (data.offsets.length > 8) offsetBars.append(element('p', `${data.offsets.length - 8} additional offsets appear in the JSON report.`, 'help'));
    byId('status').textContent = data.invalid_timestamps ? `${number(data.invalid_timestamps)} invalid timestamps excluded. Details in the JSON report.` : '';
    invalidatePlan();
  }
  function renderField(data) {
    details = data;
    const stats = byId('field-stats'); stats.replaceChildren();
    const chart = byId('value-chart'); chart.replaceChildren();
    const svg = (tag, attributes) => { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attributes).forEach(([key,value]) => node.setAttribute(key,value)); return node; };
    if (!data) {
      const label = svg('text',{x:360,y:95,'text-anchor':'middle',class:'chart-label'}); label.textContent = 'Choose an observation to inspect.'; chart.append(label);
      byId('coverage-chart').replaceChildren(); byId('chart-dates').replaceChildren();
      return;
    }
    [['First value', data.first ? data.first.value : null], ['Last value', data.last ? data.last.value : null], ['Difference', data.difference], ['Decreases / invalid values', `${number(data.decreases)} / ${number(data.invalid_count)}`]].forEach(([label,value]) => {
      const cell = element('div'); cell.append(element('span', label), element('strong', number(value))); stats.append(cell);
    });
    const finiteBuckets = data.buckets.filter(bucket => bucket.minimum !== null);
    const low = data.minimum ?? 0, high = data.maximum ?? 1;
    const span = high - low || 1;
    const y = value => 155 - (value - low) / span * 130;
    [25,90,155].forEach(height => chart.append(svg('line',{x1:0,y1:height,x2:720,y2:height,class:'chart-grid'})));
    const x = index => 6 + index / Math.max(1,data.buckets.length - 1) * 708;
    let path = '', connected = false;
    data.buckets.forEach((bucket,index) => {
      if (bucket.minimum === null) { connected = false; return; }
      chart.append(svg('line',{x1:x(index),x2:x(index),y1:y(bucket.minimum),y2:y(bucket.maximum),class:'chart-range'}));
      path += `${connected ? 'L' : 'M'}${x(index)},${y((bucket.minimum + bucket.maximum) / 2)} `;
      connected = true;
    });
    if (path) chart.append(svg('path',{d:path,class:'chart-path'}));
    else {const label = svg('text',{x:360,y:95,'text-anchor':'middle',class:'chart-label'}); label.textContent = 'No numeric observations in this range'; chart.append(label);}
    if (finiteBuckets.length) {
      [low,high].forEach(value => {const label = svg('text',{x:4,y:Math.max(14,y(value)-6),class:'chart-label'}); label.textContent = number(value); chart.append(label);});
    }
    const coverage = byId('coverage-chart'); coverage.replaceChildren();
    data.buckets.forEach(bucket => {const bar = element('span'); const fill = element('i'); fill.style.height = `${bucket.records ? bucket.count / bucket.records * 100 : 0}%`; bar.title = `${date(bucket.timestamp)} UTC: ${bucket.count} values in ${bucket.records} rows`; bar.append(fill); coverage.append(bar);});
    const dates = byId('chart-dates'); dates.replaceChildren();
    if (data.buckets.length) dates.append(element('span', shortDate(data.buckets[0].timestamp)), element('span', shortDate(data.buckets[data.buckets.length-1].timestamp)));
  }
  async function loadField() {
    const current = ++fieldRequest;
    if (!byId('chart-field').value) {renderField(null); return;}
    try { const response = await request(`/api/field?name=${encodeURIComponent(byId('chart-field').value)}`); const data = await response.json(); if (current === fieldRequest) renderField(data); }
    catch (error) { showError(error); }
  }
  function renderPlan(data) {
    plan = data;
    byId('plan-result').hidden = false;
    byId('export-result').hidden = true;
    byId('ready-only').checked = false;
    byId('plan-ready').textContent = number(data.totals.ready);
    byId('plan-blocked').textContent = number(data.totals.blocked);
    byId('plan-deleted').textContent = number(data.totals.source_rows_deleted);
    byId('plan-retained').textContent = number(data.totals.source_rows_retained);
    const destination = data.options.direction === 'record' ? 'nearest existing record' : `${data.options.direction} boundary on a ${data.options.interval}s grid`;
    byId('plan-description').textContent = `${data.options.fields.join(', ')} → ${destination}. Maximum shift: ${data.options.tolerance}s. ${number(data.totals.values_transferred)} values into ${number(data.totals.ready_destinations)} destination records.`;
    const reasonList = byId('reason-list'); reasonList.replaceChildren();
    Object.entries(data.reasons).forEach(([reason,count]) => reasonList.append(element('span', `${reasons[reason] || reason}: ${number(count)}`)));
    byId('preview-filter').value = data.preview.some(entry => entry.status === 'blocked') ? 'blocked' : 'all';
    previewPage = 0;
    renderPreview();
    byId('preview-note').textContent = `Preview includes ${number(data.preview.length)} of ${number(data.totals.destinations)} destination groups. Checked all ${number(data.totals.candidates)} source rows.`;
    byId('ready-only-label').hidden = !data.totals.blocked;
    byId('status').textContent = 'Dry run complete.';
    updateExportButton();
  }
  function renderPreview() {
    if (!plan) return;
    const filter = byId('preview-filter').value;
    const entries = plan.preview.filter(entry => filter === 'all' || entry.status === filter);
    const pageSize = Number(byId('preview-page-size').value);
    const pages = Math.ceil(entries.length / pageSize);
    previewPage = Math.max(0, Math.min(previewPage, pages - 1));
    const start = previewPage * pageSize;
    const rows = byId('preview-rows'); rows.replaceChildren();
    entries.slice(start, start + pageSize).forEach(entry => {
      const row = element('tr'); const outcome = element('td'); outcome.append(element('span',entry.status === 'ready' ? 'Ready' : 'Blocked', `outcome ${entry.status}`));
      if (entry.reasons.length) outcome.append(element('span',entry.reasons.map(reason => reasons[reason] || reason).join('; '),'reason-text'));
      const timestamps = element('td'); timestamps.append(element('div',date(entry.source_timestamp)),element('div',`→ ${date(entry.target_timestamp)}`)); if (entry.source_count > 1) timestamps.append(element('div',`${entry.source_count} source readings`));
      const values = element('td'); entry.changes.forEach(change => {values.append(element('div', `${change.field}: ${number(change.before)} → ${entry.status === 'ready' ? number(change.after) : 'not applied'}`, 'preview-value')); if (change.input_count > 1) values.append(element('div',`${change.reducer} of ${change.inputs.map(number).join(', ')}${change.input_count > change.inputs.length ? ` … (${change.input_count} values)` : ''}`,'help'));});
      const after = entry.status !== 'ready' ? 'Unchanged' : entry.source_count > 1 ? `${entry.deleted_source_count} rows removed; ${entry.retained_source_count} retained${entry.retained_fields.length ? ` with ${entry.retained_fields.join(', ')}` : ''}` : entry.retained_fields.length ? `Transfer values cleared; keep ${entry.retained_fields.join(', ')}` : 'Row removed after transfer';
      row.append(outcome,timestamps,values,element('td',after)); rows.append(row);
    });
    if (!entries.length) {
      const row = element('tr'); const cell = element('td', filter === 'blocked' ? 'No conflicts in this preview.' : filter === 'ready' ? 'No ready groups in this preview.' : 'No groups to preview.', 'preview-empty');
      cell.colSpan = 4; row.append(cell); rows.append(row);
    }
    byId('preview-range').textContent = entries.length ? `${number(start + 1)}–${number(Math.min(start + pageSize, entries.length))} of ${number(entries.length)} preview groups` : '0 preview groups';
    byId('preview-page').textContent = pages ? `Page ${previewPage + 1} of ${pages}` : 'Page 0 of 0';
    byId('preview-previous').disabled = previewPage === 0;
    byId('preview-next').disabled = previewPage + 1 >= pages;
  }
  async function downloadReport(extension) {
    try {
      const response = await request(`/api/report.${extension}?field=${encodeURIComponent(byId('chart-field').value)}`);
      const url = URL.createObjectURL(await response.blob()); const link = element('a'); link.href = url; link.download = `archive-report.${extension}`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
    } catch (error) { showError(error); }
  }
  async function start() {
    ['preview-filter','preview-page-size'].forEach(id => byId(id).addEventListener('change', () => {previewPage = 0; renderPreview();}));
    byId('preview-previous').addEventListener('click', () => {previewPage -= 1; renderPreview();});
    byId('preview-next').addEventListener('click', () => {previewPage += 1; renderPreview();});
    try {
      if (offline) {
        const saved = JSON.parse(embedded.textContent); renderArchive(saved.report); if (saved.details) {byId('chart-field').value = saved.details.field; renderField(saved.details);} if (saved.plan) {
          renderPlan(saved.plan);
          document.querySelectorAll('.transfer-field').forEach(input => {input.checked = saved.plan.options.fields.includes(input.value);});
          document.querySelectorAll('.field-reducer').forEach(select => {const method = (saved.plan.options.reducers || {})[select.dataset.field] || saved.plan.options.reducer; if (method && method !== 'reject') select.value = method;});
          byId('selected-count').textContent = `${saved.plan.options.fields.length} selected`;
        }
        document.querySelectorAll('button,input,select').forEach(control => {if (!control.closest('#preview-controls, #preview-pagination')) control.disabled = true;});
        byId('merge-form').hidden = true; byId('saved-report-note').hidden = false; byId('export-panel').hidden = true; byId('download-html').hidden = true; byId('download-json').hidden = true; byId('create-backup').hidden = true;
        byId('status').textContent = '';
        return;
      }
      renderArchive(await (await request('/api/report')).json());
      byId('chart-field').addEventListener('change',loadField);
      byId('merge-form').addEventListener('input',invalidatePlan);
      byId('merge-form').addEventListener('change',invalidatePlan);
      byId('ready-only').addEventListener('change',updateExportButton);
      byId('download-html').addEventListener('click',() => downloadReport('html'));
      byId('download-json').addEventListener('click',() => downloadReport('json'));
      byId('create-backup').addEventListener('click', async () => {
        if (busy) return; clearError(); setBusy(true);
        byId('status').textContent = 'Saving backup…';
        try {
          renderBackup(await (await request('/api/backup', {method:'POST',body:'{}'})).json());
          byId('status').textContent = '';
        } catch (error) { showError(error); }
        finally { setBusy(false); }
      });
      byId('merge-form').addEventListener('submit',async event => {
        event.preventDefault(); if (busy) return; clearError(); plan = null; updateExportButton();
        if (!selectedFields().length) {showError(new Error('Choose at least one observation in the table above.')); return;}
        const methods = Array.from(document.querySelectorAll('.field-reducer')).filter(select => select.value && selectedFields().includes(select.dataset.field));
        const reducers = {};
        methods.forEach(select => {reducers[select.dataset.field] = select.value;});
        setBusy(true); byId('status').textContent = 'Running dry run…';
        try {
          const start = byId('plan-start').value ? Date.parse(byId('plan-start').value + 'Z') / 1000 : report.start;
          const stop = byId('plan-stop').value ? Date.parse(byId('plan-stop').value + 'Z') / 1000 : report.stop;
          const response = await request('/api/plan',{method:'POST',body:JSON.stringify({fields:selectedFields(),anchor:byId('anchor').value,interval:Number(byId('merge-interval').value),tolerance:Number(byId('tolerance').value),direction:byId('direction').value,start,stop,reducers})});
          renderPlan(await response.json());
        } catch (error) {byId('plan-result').hidden = true; showError(error);}
        finally {setBusy(false);}
      });
      byId('export-copy').addEventListener('click',async () => {
        if (!plan || busy) return; clearError(); setBusy(true); byId('status').textContent = 'Backing up and exporting…';
        try {
          const response = await request('/api/export',{method:'POST',body:JSON.stringify({plan_id:plan.plan_id,ready_only:byId('ready-only').checked})}); const result = await response.json();
          renderBackup(result.backup);
          const output = byId('export-result'); output.replaceChildren(element('div',`${number(result.totals.ready)} records merged. ${number(result.after.records)} archive rows. ${number(result.daily_summaries_rebuilt.length)} daily summary rows rebuilt.`));
          const link = element('a','Download corrected database'); link.href = result.download; link.download = 'archive-corrected.sdb'; output.append(link); output.hidden = false; byId('status').textContent = 'Download the corrected copy before stopping this session.';
        } catch (error) {showError(error);}
        finally {setBusy(false);}
      });
    } catch (error) { byId('status').textContent = 'The archive could not be loaded.'; showError(error); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',start); else start();
})();
