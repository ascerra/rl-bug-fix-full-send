/* Action Map — D3.js layered visualization.
 *
 * Renders a horizontal layered map from JSON data produced by
 * engine.visualization.action_map.build_action_map().
 *
 * Each layer = a phase/iteration. Nodes = actions. Edges = data flow.
 * Node size encodes token usage; color encodes action type.
 *
 * Usage: renderActionMap(mapData, 'container-id', 'detail-panel-id');
 */

function renderActionMap(data, containerId, detailPanelId) {
  var container = document.getElementById(containerId);
  var detailPanel = document.getElementById(detailPanelId);
  if (!container || !data) return;

  var layers = data.layers || [];
  if (layers.length === 0) {
    container.innerHTML = '<div class="empty-state">No action map data available.</div>';
    return;
  }

  var LAYER_PADDING = 40;
  var NODE_MIN_R = 10;
  var NODE_MAX_R = 28;
  var NODE_SPACING_Y = 60;
  var LAYER_WIDTH = 180;
  var HEADER_HEIGHT = 36;
  var TOP_MARGIN = 20;
  var LEFT_MARGIN = 30;

  var maxTokens = 1;
  layers.forEach(function(layer) {
    layer.nodes.forEach(function(n) {
      if (n.tokens > maxTokens) maxTokens = n.tokens;
    });
  });

  var maxNodesInLayer = 1;
  layers.forEach(function(layer) {
    if (layer.nodes.length > maxNodesInLayer) maxNodesInLayer = layer.nodes.length;
  });

  var totalWidth = LEFT_MARGIN + layers.length * (LAYER_WIDTH + LAYER_PADDING) + LEFT_MARGIN;
  var totalHeight = TOP_MARGIN + HEADER_HEIGHT + maxNodesInLayer * NODE_SPACING_Y + NODE_SPACING_Y;
  if (totalHeight < 200) totalHeight = 200;

  var containerWidth = container.clientWidth || 960;
  if (totalWidth < containerWidth) totalWidth = containerWidth;

  var svg = d3.select(container).append('svg')
    .attr('width', totalWidth)
    .attr('height', totalHeight)
    .style('font', '12px sans-serif');

  var defs = svg.append('defs');
  defs.append('marker')
    .attr('id', 'action-map-arrow')
    .attr('viewBox', '0 0 10 10')
    .attr('refX', 10)
    .attr('refY', 5)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M 0 0 L 10 5 L 0 10 z')
    .attr('fill', '#30363d');

  var TYPE_COLORS = {
    'llm_query': '#58a6ff',
    'tool_execution': '#3fb950',
    'escalation': '#f85149',
    'unknown': '#8b949e'
  };

  var PHASE_COLORS = {
    'triage': 'rgba(88,166,255,0.08)',
    'implement': 'rgba(63,185,80,0.08)',
    'review': 'rgba(188,140,255,0.08)',
    'validate': 'rgba(210,153,34,0.08)',
    'report': 'rgba(139,148,158,0.08)'
  };

  var PHASE_BORDER_COLORS = {
    'triage': 'rgba(88,166,255,0.3)',
    'implement': 'rgba(63,185,80,0.3)',
    'review': 'rgba(188,140,255,0.3)',
    'validate': 'rgba(210,153,34,0.3)',
    'report': 'rgba(139,148,158,0.3)'
  };

  function nodeRadius(tokens) {
    if (tokens <= 0) return NODE_MIN_R;
    var ratio = Math.sqrt(tokens / maxTokens);
    return NODE_MIN_R + ratio * (NODE_MAX_R - NODE_MIN_R);
  }

  function nodeColor(d) {
    return TYPE_COLORS[d.action_type] || TYPE_COLORS['unknown'];
  }

  var nodePositions = {};

  layers.forEach(function(layer, li) {
    var layerX = LEFT_MARGIN + li * (LAYER_WIDTH + LAYER_PADDING);
    var layerHeight = Math.max(layer.nodes.length * NODE_SPACING_Y + NODE_SPACING_Y, 80);
    var bgColor = PHASE_COLORS[layer.phase] || 'rgba(139,148,158,0.05)';
    var borderColor = PHASE_BORDER_COLORS[layer.phase] || 'rgba(139,148,158,0.2)';

    svg.append('rect')
      .attr('x', layerX)
      .attr('y', TOP_MARGIN)
      .attr('width', LAYER_WIDTH)
      .attr('height', layerHeight + HEADER_HEIGHT)
      .attr('rx', 6)
      .attr('fill', bgColor)
      .attr('stroke', borderColor)
      .attr('stroke-width', 1);

    svg.append('text')
      .attr('x', layerX + LAYER_WIDTH / 2)
      .attr('y', TOP_MARGIN + 22)
      .attr('text-anchor', 'middle')
      .attr('fill', '#e6edf3')
      .attr('font-weight', '600')
      .attr('font-size', '11px')
      .text(layer.phase.toUpperCase() + ' #' + layer.iteration);

    var statusIndicator = layer.successful ? '\u2713' : '\u2717';
    var statusColor = layer.successful ? '#3fb950' : '#f85149';
    svg.append('text')
      .attr('x', layerX + LAYER_WIDTH - 12)
      .attr('y', TOP_MARGIN + 22)
      .attr('text-anchor', 'end')
      .attr('fill', statusColor)
      .attr('font-size', '13px')
      .attr('font-weight', 'bold')
      .text(statusIndicator);

    layer.nodes.forEach(function(node, ni) {
      var cx = layerX + LAYER_WIDTH / 2;
      var cy = TOP_MARGIN + HEADER_HEIGHT + NODE_SPACING_Y / 2 + ni * NODE_SPACING_Y;
      var r = nodeRadius(node.tokens);

      nodePositions[node.id] = { x: cx, y: cy, r: r, layerIndex: li };

      var g = svg.append('g')
        .attr('cursor', 'pointer')
        .on('click', function() { showDetail(node); });

      g.append('circle')
        .attr('cx', cx)
        .attr('cy', cy)
        .attr('r', r)
        .attr('fill', node.status === 'success' ? nodeColor(node) : '#0d1117')
        .attr('stroke', nodeColor(node))
        .attr('stroke-width', node.status === 'success' ? 1.5 : 2.5)
        .attr('opacity', 0.9);

      var typeIcons = {
        'llm_query': 'AI',
        'tool_execution': 'T',
        'escalation': '!'
      };
      g.append('text')
        .attr('x', cx)
        .attr('y', cy)
        .attr('dy', '0.35em')
        .attr('text-anchor', 'middle')
        .attr('fill', '#e6edf3')
        .attr('font-size', '9px')
        .attr('font-weight', '600')
        .text(typeIcons[node.action_type] || '?');

      var descText = node.description;
      if (descText.length > 25) descText = descText.substring(0, 22) + '...';
      g.append('text')
        .attr('x', cx)
        .attr('y', cy + r + 12)
        .attr('text-anchor', 'middle')
        .attr('fill', '#8b949e')
        .attr('font-size', '9px')
        .text(descText);

      g.append('title')
        .text(node.action_type + ': ' + node.description
          + '\nStatus: ' + node.status
          + '\nTokens: ' + node.tokens
          + '\nDuration: ' + formatDuration(node.duration_ms));
    });
  });

  var edgesGroup = svg.append('g').lower();

  var EDGE_COLORS = {
    'sequential': '#30363d',
    'phase_transition': '#58a6ff',
    'data_flow': '#d29922'
  };

  (data.edges || []).forEach(function(edge) {
    var s = nodePositions[edge.source];
    var t = nodePositions[edge.target];
    if (!s || !t) return;

    var color = EDGE_COLORS[edge.type] || '#30363d';
    var opacity = edge.type === 'data_flow' ? 0.6 : 0.4;
    var dasharray = edge.type === 'data_flow' ? '4,3' : 'none';

    edgesGroup.append('path')
      .attr('d', buildEdgePath(s, t))
      .attr('fill', 'none')
      .attr('stroke', color)
      .attr('stroke-width', edge.type === 'phase_transition' ? 1.5 : 1)
      .attr('stroke-opacity', opacity)
      .attr('stroke-dasharray', dasharray)
      .attr('marker-end', 'url(#action-map-arrow)');
  });

  function buildEdgePath(s, t) {
    var sx = s.x + s.r;
    var sy = s.y;
    var tx = t.x - t.r;
    var ty = t.y;

    if (s.layerIndex === t.layerIndex) {
      sx = s.x;
      sy = s.y + s.r;
      tx = t.x;
      ty = t.y - t.r;
      var midY = (sy + ty) / 2;
      return 'M' + sx + ',' + sy
        + 'C' + sx + ',' + midY + ' ' + tx + ',' + midY + ' ' + tx + ',' + ty;
    }

    var midX = (sx + tx) / 2;
    return 'M' + sx + ',' + sy
      + 'C' + midX + ',' + sy + ' ' + midX + ',' + ty + ' ' + tx + ',' + ty;
  }

  function showDetail(node) {
    if (!detailPanel) return;
    var meta = node.meta || {};
    var html = '';

    html += '<h3>' + escapeHtml(node.description) + '</h3>';

    /* Status bar */
    var statusColor = node.status === 'success' ? '#3fb950' : '#f85149';
    var statusClass = node.status === 'success' ? 'status-success' : 'status-failure';
    html += '<div class="detail-status-bar">';
    html += '<span class="badge ' + statusClass + '">' + escapeHtml(node.status) + '</span>';
    html += '<span class="action-type action-type-' + escapeHtml(node.action_type) + '">' + escapeHtml(node.action_type.replace(/_/g, ' ')) + '</span>';
    html += '<span class="detail-duration">' + escapeHtml(formatDuration(node.duration_ms));
    if (node.tokens > 0) html += ' · ' + node.tokens + ' tokens';
    html += '</span>';
    html += '</div>';

    /* Human-readable narrative */
    html += '<div class="detail-narrative">';
    var desc = (meta.full_description || node.description || '').toLowerCase();
    var input = meta.input || {};
    var output = meta.output || {};

    if (node.action_type === 'llm_query') {
      html += '<p>The agent asked the AI model to <strong>' + escapeHtml((meta.full_description || node.description).toLowerCase()) + '</strong>.</p>';
      var prov = meta.provenance || {};
      if (prov.model) {
        html += '<p>Model: <strong>' + escapeHtml(prov.model) + '</strong>';
        if (prov.provider) html += ' via ' + escapeHtml(prov.provider);
        html += '</p>';
      }
      var ctx = meta.llm_context || {};
      if (ctx.tokens_in || ctx.tokens_out) {
        html += '<p>' + (ctx.tokens_in || 0) + ' tokens in, ' + (ctx.tokens_out || 0) + ' tokens out</p>';
      }
      if (output.success === false && output.error) {
        html += '<p style="color:var(--failure)">Failed: ' + escapeHtml(truncate(output.error, 200)) + '</p>';
      }
    } else if (node.action_type === 'tool_execution') {
      html += '<p>' + describeToolAction(desc, input) + '</p>';
      var filePath = input.path || (input.context && input.context.path) || '';
      if (filePath) html += '<p>File: <span class="detail-file-path">' + escapeHtml(filePath) + '</span></p>';
      if (output.success) {
        html += '<p style="color:var(--success)">Completed successfully</p>';
      } else if (output.success === false) {
        html += '<p style="color:var(--failure)">Failed';
        if (output.error) html += ': ' + escapeHtml(truncate(output.error, 200));
        html += '</p>';
      }
    } else if (node.action_type === 'escalation') {
      html += '<p style="color:var(--failure)"><strong>The agent escalated this issue for human review.</strong></p>';
    } else {
      html += '<p>' + escapeHtml(meta.full_description || node.description) + '</p>';
    }
    html += '</div>';

    /* Readable output */
    var outputDisplay = extractReadableOutput(output);
    if (outputDisplay) {
      html += '<div class="detail-section">';
      html += '<div class="detail-section-title">Result</div>';
      html += '<div class="detail-code-block">' + escapeHtml(outputDisplay) + '</div>';
      html += '</div>';
    }

    /* Phase context */
    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">Context</div>';
    html += '<div class="detail-kv-list">';
    html += kvRow('Phase', node.phase);
    html += kvRow('Iteration', '#' + node.iteration);
    if (node.duration_ms) html += kvRow('Duration', formatDuration(node.duration_ms));
    if (meta.timestamp) html += kvRow('Timestamp', meta.timestamp.substring(0, 19));
    html += '</div></div>';

    /* Extra structured details */
    if (meta) {
      html += renderExtraFields(meta);
    }

    detailPanel.innerHTML = html;
    detailPanel.style.display = 'block';
  }

  function describeToolAction(desc, input) {
    if (desc.indexOf('read file') >= 0 || desc.indexOf('file_read') >= 0) return 'Read the contents of a file to understand the existing code.';
    if (desc.indexOf('write file') >= 0 || desc.indexOf('file_write') >= 0) return 'Wrote changes to a file as part of the fix.';
    if (desc.indexOf('search') >= 0 || desc.indexOf('file_search') >= 0) return 'Searched the codebase for relevant code patterns.';
    if (desc.indexOf('shell') >= 0 || desc.indexOf('run:') >= 0) return 'Ran a shell command (e.g. tests, linters, or build tools).';
    if (desc.indexOf('git diff') >= 0) return 'Checked the git diff to review what changed.';
    if (desc.indexOf('git commit') >= 0) return 'Committed the changes to git.';
    if (desc.indexOf('github') >= 0 || desc.indexOf('gh issue') >= 0) return 'Called the GitHub API to fetch issue details or interact with the repository.';
    if (desc.indexOf('find') >= 0) return 'Searched the file tree to locate relevant source files.';
    if (desc.indexOf('go test') >= 0 || desc.indexOf('pytest') >= 0 || desc.indexOf('npm test') >= 0) return 'Ran the test suite to check if the fix works.';
    if (desc.indexOf('ruff') >= 0 || desc.indexOf('lint') >= 0 || desc.indexOf('golangci') >= 0) return 'Ran linting to check code quality.';
    return 'Executed a tool action.';
  }

  function extractReadableOutput(output) {
    if (!output || typeof output !== 'object') return '';
    var data = output.data || output;
    if (typeof data === 'string') return data.length > 2000 ? data.substring(0, 2000) + '\n... (truncated)' : data;
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
    var skip = {'full_description':1,'input':1,'output':1,'llm_context':1,'provenance':1,'timestamp':1};
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
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}
