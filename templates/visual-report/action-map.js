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
    var html = '<h3>' + escapeHtml(node.description) + '</h3>';
    html += '<p>'
      + '<span class="action-type action-type-' + escapeHtml(node.action_type) + '">'
      + escapeHtml(node.action_type) + '</span>'
      + ' &nbsp;|&nbsp; <strong>Status:</strong> '
      + '<span style="color:' + (node.status === 'success' ? '#3fb950' : '#f85149') + '">'
      + escapeHtml(node.status) + '</span>'
      + ' &nbsp;|&nbsp; <strong>Tokens:</strong> ' + node.tokens
      + ' &nbsp;|&nbsp; <strong>Duration:</strong> ' + escapeHtml(formatDuration(node.duration_ms))
      + '</p>';

    if (node.meta && Object.keys(node.meta).length > 0) {
      var filtered = filterMeta(node.meta);
      html += '<pre>' + escapeHtml(JSON.stringify(filtered, null, 2)) + '</pre>';
    }

    detailPanel.innerHTML = html;
    detailPanel.style.display = 'block';
  }

  function filterMeta(meta) {
    var out = {};
    for (var k in meta) {
      if (!meta.hasOwnProperty(k)) continue;
      var v = meta[k];
      if (typeof v === 'string' && v.length > 500) {
        out[k] = v.substring(0, 500) + '...';
      } else if (Array.isArray(v) && v.length > 10) {
        out[k] = v.slice(0, 10).concat(['... (' + v.length + ' total)']);
      } else {
        out[k] = v;
      }
    }
    return out;
  }

  function formatDuration(ms) {
    if (!ms || ms < 0) return '0ms';
    if (ms < 1000) return Math.round(ms) + 'ms';
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    var m = s / 60;
    return m.toFixed(1) + 'm';
  }

  function escapeHtml(s) {
    if (typeof s !== 'string') s = String(s);
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}
