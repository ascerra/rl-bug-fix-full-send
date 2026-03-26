/* Decision Tree — D3.js interactive collapsible tree visualization.
 *
 * Renders a horizontal collapsible tree from JSON data produced by
 * engine.visualization.decision_tree.build_decision_tree().
 *
 * Usage: renderDecisionTree(treeData, 'container-id', 'detail-panel-id');
 */

function renderDecisionTree(data, containerId, detailPanelId) {
  var container = document.getElementById(containerId);
  var detailPanel = document.getElementById(detailPanelId);
  if (!container || !data) return;

  if (!data.children || data.children.length === 0) {
    container.innerHTML = '<div class="empty-state">No decision tree data available.</div>';
    return;
  }

  var NODE_RADIUS = 7;
  var DX = 28;
  var containerWidth = container.clientWidth || 960;
  var marginTop = 10, marginRight = 200, marginBottom = 10, marginLeft = 100;

  var root = d3.hierarchy(data);

  /* Assign stable IDs and collapse action nodes by default */
  var nodeId = 0;
  root.descendants().forEach(function(d) {
    d._nodeId = ++nodeId;
    if (d.data.type === 'action' && d.children) {
      d._children = d.children;
      d.children = null;
    }
  });

  var dy = Math.max(180, (containerWidth - marginLeft - marginRight) / (1 + root.height));
  var tree = d3.tree().nodeSize([DX, dy]);

  var svg = d3.select(container).append('svg')
    .attr('class', 'decision-tree-svg')
    .style('font', '12px sans-serif')
    .style('user-select', 'none');

  var gLink = svg.append('g')
    .attr('fill', 'none')
    .attr('stroke', '#30363d')
    .attr('stroke-opacity', 0.6)
    .attr('stroke-width', 1.5);

  var gNode = svg.append('g')
    .attr('cursor', 'pointer')
    .attr('pointer-events', 'all');

  function diagonal(s, d) {
    return 'M' + s.y + ',' + s.x
      + 'C' + (s.y + d.y) / 2 + ',' + s.x
      + ' ' + (s.y + d.y) / 2 + ',' + d.x
      + ' ' + d.y + ',' + d.x;
  }

  var STATUS_COLORS = {
    'success': '#3fb950',
    'failure': '#f85149',
    'retry': '#d29922',
    'escalated': '#bc8cff',
    'timeout': '#d29922',
    'unknown': '#8b949e'
  };

  function nodeColor(d) {
    return STATUS_COLORS[d.data.status] || STATUS_COLORS['unknown'];
  }

  function nodeStrokeWidth(d) {
    return (d.data.type === 'root' || d.data.type === 'outcome') ? 2.5 : 1.5;
  }

  function nodeShape(d) {
    return (d.data.type === 'outcome') ? 5 : NODE_RADIUS;
  }

  root.x0 = DX / 2;
  root.y0 = 0;

  function update(source) {
    var duration = 300;
    var nodes = root.descendants().reverse();
    var links = root.links();

    tree(root);

    var x0 = Infinity, x1 = -Infinity;
    root.each(function(d) {
      if (d.x > x1) x1 = d.x;
      if (d.x < x0) x0 = d.x;
    });

    var height = x1 - x0 + marginTop + marginBottom + DX;
    var treeWidth = 0;
    root.each(function(d) { if (d.y > treeWidth) treeWidth = d.y; });
    var totalWidth = treeWidth + marginLeft + marginRight;
    if (totalWidth < containerWidth) totalWidth = containerWidth;

    var transition = svg.transition()
      .duration(duration)
      .attr('height', height)
      .attr('width', totalWidth)
      .attr('viewBox', [
        -marginLeft,
        x0 - marginTop,
        totalWidth,
        height
      ].join(' '));

    /* ---- Nodes ---- */
    var node = gNode.selectAll('g').data(nodes, function(d) { return d._nodeId; });

    var nodeEnter = node.enter().append('g')
      .attr('transform', function() {
        return 'translate(' + (source.y0 || 0) + ',' + (source.x0 || 0) + ')';
      })
      .attr('fill-opacity', 0)
      .attr('stroke-opacity', 0)
      .on('click', function(event, d) {
        if (d.children) {
          d._children = d.children;
          d.children = null;
        } else if (d._children) {
          d.children = d._children;
          d._children = null;
        }
        update(d);
        showDetail(d);
      });

    nodeEnter.append('circle')
      .attr('r', function(d) { return nodeShape(d); })
      .attr('fill', function(d) { return d._children ? nodeColor(d) : '#0d1117'; })
      .attr('stroke', nodeColor)
      .attr('stroke-width', nodeStrokeWidth);

    nodeEnter.append('text')
      .attr('dy', '0.31em')
      .attr('x', function(d) { return (d._children || d.children) ? -12 : 12; })
      .attr('text-anchor', function(d) { return (d._children || d.children) ? 'end' : 'start'; })
      .text(function(d) { return d.data.label; })
      .attr('fill', '#e6edf3')
      .attr('font-size', function(d) { return d.data.type === 'action' ? '11px' : '12px'; })
      .clone(true).lower()
      .attr('stroke-linejoin', 'round')
      .attr('stroke-width', 3)
      .attr('stroke', '#0d1117');

    var nodeUpdate = node.merge(nodeEnter).transition(transition)
      .attr('transform', function(d) { return 'translate(' + d.y + ',' + d.x + ')'; })
      .attr('fill-opacity', 1)
      .attr('stroke-opacity', 1);

    nodeUpdate.select('circle')
      .attr('fill', function(d) { return d._children ? nodeColor(d) : '#0d1117'; })
      .attr('stroke', nodeColor);

    node.exit().transition(transition).remove()
      .attr('transform', function() {
        return 'translate(' + source.y + ',' + source.x + ')';
      })
      .attr('fill-opacity', 0)
      .attr('stroke-opacity', 0);

    /* ---- Links ---- */
    var link = gLink.selectAll('path').data(links, function(d) { return d.target._nodeId; });

    link.enter().append('path')
      .attr('d', function() {
        var o = {x: source.x0 || 0, y: source.y0 || 0};
        return diagonal(o, o);
      })
      .merge(link).transition(transition)
      .attr('d', function(d) { return diagonal(d.source, d.target); });

    link.exit().transition(transition).remove()
      .attr('d', function() {
        var o = {x: source.x, y: source.y};
        return diagonal(o, o);
      });

    root.eachBefore(function(d) {
      d.x0 = d.x;
      d.y0 = d.y;
    });
  }

  function showDetail(d) {
    if (!detailPanel) return;
    var data = d.data;
    var meta = data.meta || {};
    var html = '';

    html += '<h3>' + escapeHtml(data.label) + '</h3>';

    /* Status bar with badges */
    var statusColor = STATUS_COLORS[data.status] || '#8b949e';
    var statusClass = 'status-' + data.status;
    html += '<div class="detail-status-bar">';
    html += '<span class="badge ' + statusClass + '">' + escapeHtml(data.status) + '</span>';
    html += '<span class="badge action-type-' + escapeHtml(data.type) + '" style="background:rgba(139,148,158,0.12);color:' + statusColor + '">' + escapeHtml(data.type) + '</span>';
    if (meta.duration_ms) {
      html += '<span class="detail-duration">' + formatDuration(meta.duration_ms) + '</span>';
    }
    html += '</div>';

    /* Render type-specific human-readable content */
    if (data.type === 'action') {
      html += renderActionDetail(meta);
    } else if (data.type === 'phase') {
      html += renderPhaseDetail(meta);
    } else if (data.type === 'root') {
      html += renderRootDetail(meta);
    } else if (data.type === 'outcome') {
      html += renderOutcomeDetail(meta, data.status);
    } else {
      html += renderGenericDetail(meta);
    }

    /* Extra structured details for actions */
    if (data.type === 'action' && meta) {
      html += renderExtraFields(meta);
    }

    detailPanel.innerHTML = html;
    detailPanel.style.display = 'block';
  }

  function renderActionDetail(meta) {
    var html = '<div class="detail-narrative">';
    var actionType = meta.action_type || 'unknown';
    var desc = meta.description || 'No description';
    var input = meta.input || {};
    var output = meta.output || {};

    if (actionType === 'llm_query') {
      html += '<p>The agent asked the AI model to <strong>' + escapeHtml(desc.toLowerCase()) + '</strong>.</p>';
      if (meta.provenance) {
        var prov = meta.provenance;
        if (prov.model) {
          html += '<p>Model: <strong>' + escapeHtml(prov.model) + '</strong>';
          if (prov.provider) html += ' via ' + escapeHtml(prov.provider);
          html += '</p>';
        }
      }
      var ctx = meta.llm_context || {};
      if (ctx.tokens_in || ctx.tokens_out) {
        html += '<p>' + (ctx.tokens_in || 0) + ' tokens in, ' + (ctx.tokens_out || 0) + ' tokens out</p>';
      }
      if (output.success === false && output.error) {
        html += '<p style="color:var(--failure)">Failed: ' + escapeHtml(truncate(output.error, 200)) + '</p>';
      }
    } else if (actionType.indexOf('tool') >= 0 || actionType === 'tool_execution') {
      var toolName = extractToolName(desc);
      var filePath = input.path || input.context && input.context.path || '';
      html += '<p>' + describeToolAction(toolName, desc, filePath) + '</p>';
      if (filePath) {
        html += '<p>File: <span class="detail-file-path">' + escapeHtml(filePath) + '</span></p>';
      }
      if (output.success) {
        html += '<p style="color:var(--success)">Completed successfully</p>';
      } else if (output.success === false) {
        html += '<p style="color:var(--failure)">Failed';
        if (output.error) html += ': ' + escapeHtml(truncate(output.error, 200));
        html += '</p>';
      }
    } else if (actionType === 'escalation') {
      html += '<p style="color:var(--failure)"><strong>The agent escalated this issue for human review.</strong></p>';
      if (desc) html += '<p>' + escapeHtml(desc) + '</p>';
    } else {
      html += '<p>' + escapeHtml(desc) + '</p>';
    }
    html += '</div>';

    /* Show readable output data if available */
    var outputDisplay = extractReadableOutput(output);
    if (outputDisplay) {
      html += '<div class="detail-section">';
      html += '<div class="detail-section-title">Result</div>';
      html += '<div class="detail-code-block">' + escapeHtml(outputDisplay) + '</div>';
      html += '</div>';
    }

    return html;
  }

  function renderPhaseDetail(meta) {
    var html = '<div class="detail-narrative">';
    var phase = meta.phase || 'unknown';
    var iter = meta.iteration || 0;

    var phaseDescriptions = {
      'triage': 'The agent analyzed the issue to understand the bug, classify its severity, and identify which files are likely affected.',
      'implement': 'The agent attempted to write a code fix based on the triage analysis and any prior review feedback.',
      'review': 'The agent independently re-read the issue and reviewed the proposed changes for correctness, security, and scope.',
      'validate': 'The agent ran tests and linters to verify the fix works correctly without breaking anything.',
      'report': 'The agent generated visual reports and summaries of the execution.'
    };

    html += '<p>' + (phaseDescriptions[phase] || 'Phase: ' + escapeHtml(phase)) + '</p>';

    if (meta.success) {
      html += '<p style="color:var(--success)">This phase completed successfully.</p>';
    } else if (meta.escalate) {
      html += '<p style="color:var(--escalated)">This phase escalated the issue for human review.</p>';
    } else if (meta.next_phase && meta.next_phase !== phase) {
      html += '<p style="color:var(--warning)">Phase did not succeed — transitioning to <strong>' + escapeHtml(meta.next_phase) + '</strong>.</p>';
    }
    html += '</div>';

    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">Activity</div>';
    html += '<div class="detail-kv-list">';
    html += kvRow('Iteration', '#' + iter);
    html += kvRow('Actions taken', meta.action_count || 0);
    html += kvRow('LLM calls', meta.llm_call_count || 0);
    html += kvRow('Tool calls', meta.tool_call_count || 0);
    if (meta.duration_ms) html += kvRow('Duration', formatDuration(meta.duration_ms));
    if (meta.started_at) html += kvRow('Started', meta.started_at.substring(0, 19));
    html += '</div></div>';

    return html;
  }

  function renderRootDetail(meta) {
    var html = '<div class="detail-narrative">';
    html += '<p>This is the top-level Ralph Loop execution.</p>';
    if (meta.trigger && meta.trigger.source_url) {
      html += '<p>Triggered by: <a href="' + escapeHtml(meta.trigger.source_url) + '" style="color:var(--accent)">' + escapeHtml(meta.trigger.source_url) + '</a></p>';
    }
    html += '</div>';

    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">Overview</div>';
    html += '<div class="detail-kv-list">';
    html += kvRow('Execution ID', (meta.execution_id || '').substring(0, 12));
    html += kvRow('Total iterations', meta.total_iterations || 0);
    html += kvRow('Total tokens', meta.total_tokens || 0);
    if (meta.started_at) html += kvRow('Started', meta.started_at.substring(0, 19));
    html += '</div></div>';

    return html;
  }

  function renderOutcomeDetail(meta, status) {
    var html = '<div class="detail-narrative">';
    var outcomeMessages = {
      'success': 'The agent successfully completed the bug fix. All phases passed and the fix was validated.',
      'failure': 'The agent was unable to complete the bug fix after all available attempts.',
      'escalated': 'The agent determined this issue needs human attention and escalated it.',
      'timeout': 'The agent ran out of time before completing the fix.'
    };
    html += '<p>' + (outcomeMessages[status] || 'Final status: ' + escapeHtml(status)) + '</p>';
    html += '</div>';

    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">Summary</div>';
    html += '<div class="detail-kv-list">';
    html += kvRow('Final status', status);
    html += kvRow('Total iterations', meta.total_iterations || 0);
    if (meta.completed_at) html += kvRow('Completed', meta.completed_at.substring(0, 19));
    html += '</div></div>';

    return html;
  }

  function renderGenericDetail(meta) {
    if (!meta || Object.keys(meta).length === 0) {
      return '<p class="detail-hint">No additional details for this node.</p>';
    }
    var html = '<div class="detail-section">';
    html += '<div class="detail-section-title">Details</div>';
    html += '<div class="detail-kv-list">';
    for (var k in meta) {
      if (!meta.hasOwnProperty(k)) continue;
      var v = meta[k];
      if (typeof v === 'object') continue;
      html += kvRow(humanizeKey(k), v);
    }
    html += '</div></div>';
    return html;
  }

  function extractToolName(desc) {
    var match = desc.match(/^(?:\[?tool[:\s_]*)?(\w+)/i);
    return match ? match[1].toLowerCase() : '';
  }

  function describeToolAction(tool, desc, filePath) {
    var d = desc.toLowerCase();
    if (d.indexOf('read file') >= 0 || tool === 'file_read') {
      return 'Read the contents of a file' + (filePath ? ' to understand the existing code.' : '.');
    }
    if (d.indexOf('write file') >= 0 || tool === 'file_write') {
      return 'Wrote changes to a file' + (filePath ? ' as part of the fix.' : '.');
    }
    if (d.indexOf('search') >= 0 || tool === 'file_search') {
      return 'Searched the codebase for relevant code patterns.';
    }
    if (d.indexOf('shell') >= 0 || d.indexOf('run') >= 0 || tool === 'shell_run') {
      return 'Ran a shell command (e.g. tests, linters, or build tools).';
    }
    if (d.indexOf('git diff') >= 0 || tool === 'git_diff') {
      return 'Checked the git diff to review what changed.';
    }
    if (d.indexOf('git commit') >= 0 || tool === 'git_commit') {
      return 'Committed the changes to git.';
    }
    if (d.indexOf('github') >= 0 || tool === 'github_api') {
      return 'Called the GitHub API (e.g. to fetch issue details or create a PR).';
    }
    if (d.indexOf('gh issue') >= 0) {
      return 'Fetched issue details from GitHub to understand the bug report.';
    }
    if (d.indexOf('find') >= 0) {
      return 'Searched the file tree to locate relevant source files.';
    }
    if (d.indexOf('go test') >= 0 || d.indexOf('pytest') >= 0 || d.indexOf('npm test') >= 0) {
      return 'Ran the test suite to check if the fix works.';
    }
    if (d.indexOf('ruff') >= 0 || d.indexOf('lint') >= 0 || d.indexOf('golangci') >= 0) {
      return 'Ran linting to check code quality.';
    }
    return 'Executed tool: <strong>' + escapeHtml(desc.substring(0, 80)) + '</strong>';
  }

  function extractReadableOutput(output) {
    if (!output || typeof output !== 'object') return '';
    var data = output.data || output;
    if (typeof data === 'string') {
      return data.length > 2000 ? data.substring(0, 2000) + '\n... (truncated)' : data;
    }
    if (data.stdout || data.stderr || data.output) {
      var text = '';
      if (data.stdout) text += data.stdout;
      if (data.stderr) text += (text ? '\n---\n' : '') + data.stderr;
      if (data.output && typeof data.output === 'string') text += (text ? '\n---\n' : '') + data.output;
      if (!text) return '';
      return text.length > 2000 ? text.substring(0, 2000) + '\n... (truncated)' : text;
    }
    if (data.content && typeof data.content === 'string') {
      var c = data.content;
      return c.length > 1500 ? c.substring(0, 1500) + '\n... (truncated)' : c;
    }
    return '';
  }

  function kvRow(key, val) {
    return '<span class="kv-key">' + escapeHtml(humanizeKey(key)) + '</span>'
      + '<span class="kv-val">' + escapeHtml(String(val)) + '</span>';
  }

  function humanizeKey(key) {
    return key.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
  }

  function truncate(s, max) {
    if (typeof s !== 'string') return String(s);
    return s.length > max ? s.substring(0, max) + '...' : s;
  }

  function formatDuration(ms) {
    if (!ms || ms < 0) return '0ms';
    if (ms < 1000) return Math.round(ms) + 'ms';
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    var m = s / 60;
    return m.toFixed(1) + 'm';
  }

  function renderExtraFields(meta) {
    var html = '';
    var skip = {'action_type':1,'description':1,'duration_ms':1,'input':1,'output':1,'llm_context':1,'provenance':1};
    var extras = [];
    for (var k in meta) {
      if (!meta.hasOwnProperty(k) || skip[k]) continue;
      extras.push(k);
    }

    var input = meta.input || {};
    var inputExtras = {};
    var inputSkip = {'description':1,'path':1,'context':1};
    for (var ik in input) {
      if (!input.hasOwnProperty(ik) || inputSkip[ik]) continue;
      if (typeof input[ik] === 'string' && input[ik].length > 0) {
        inputExtras[ik] = input[ik];
      } else if (typeof input[ik] === 'number' || typeof input[ik] === 'boolean') {
        inputExtras[ik] = input[ik];
      }
    }

    var hasInputExtras = Object.keys(inputExtras).length > 0;
    var hasExtras = extras.length > 0;

    if (!hasInputExtras && !hasExtras) return '';

    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">More Details</div>';
    html += '<div class="detail-kv-list">';
    for (var eik in inputExtras) {
      if (!inputExtras.hasOwnProperty(eik)) continue;
      html += kvRow(eik, renderValueInline(inputExtras[eik]));
    }
    for (var ei = 0; ei < extras.length; ei++) {
      var ek = extras[ei];
      html += kvRow(ek, renderValueInline(meta[ek]));
    }
    html += '</div></div>';
    return html;
  }

  function renderValueInline(val) {
    if (val == null) return '—';
    if (typeof val === 'boolean') return val ? 'Yes' : 'No';
    if (typeof val === 'number') return String(val);
    if (typeof val === 'string') return val.length > 120 ? val.substring(0, 120) + '...' : val;
    if (Array.isArray(val)) {
      if (val.length === 0) return '(none)';
      var items = val.slice(0, 5).map(function(v) {
        return typeof v === 'string' ? v : (typeof v === 'object' ? '(' + Object.keys(v).length + ' fields)' : String(v));
      });
      return items.join(', ') + (val.length > 5 ? ' + ' + (val.length - 5) + ' more' : '');
    }
    if (typeof val === 'object') {
      var keys = Object.keys(val);
      if (keys.length === 0) return '(empty)';
      var preview = keys.slice(0, 4).map(function(k) {
        var v = val[k];
        var short = typeof v === 'string' ? (v.length > 30 ? v.substring(0, 30) + '...' : v) : String(v);
        return humanizeKey(k) + ': ' + short;
      });
      return preview.join(', ') + (keys.length > 4 ? ' + ' + (keys.length - 4) + ' more' : '');
    }
    return String(val);
  }

  function escapeHtml(s) {
    if (typeof s !== 'string') s = String(s);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  update(root);
}
