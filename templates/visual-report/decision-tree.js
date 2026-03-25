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
    var html = '<h3>' + escapeHtml(data.label) + '</h3>';
    html += '<p><strong>Type:</strong> ' + escapeHtml(data.type)
      + ' &nbsp;|&nbsp; <strong>Status:</strong> '
      + '<span style="color:' + (STATUS_COLORS[data.status] || '#8b949e') + '">'
      + escapeHtml(data.status) + '</span></p>';

    if (data.meta && Object.keys(data.meta).length > 0) {
      var filtered = filterMeta(data.meta);
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

  function escapeHtml(s) {
    if (typeof s !== 'string') s = String(s);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  update(root);
}
