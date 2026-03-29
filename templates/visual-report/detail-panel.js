/**
 * Detail Drill-Down Panel for the 3D Execution Landscape.
 *
 * Slide-in overlay panel that displays server-generated narrative HTML
 * for each action object in the scene.  No raw JSON/YAML is exposed —
 * every piece of information passes through the Python NarrativeFormatter
 * before display.
 *
 * Features:
 *   - Smooth slide-in animation from right side
 *   - Close via X button, Escape key, or clicking outside
 *   - Navigation arrows to step through actions sequentially
 *   - Keyboard shortcuts: Left/Right arrows, Escape
 *   - Renders server-generated narrative_html when available
 *   - Falls back to client-side rendering for legacy data
 */

/* global RalphSceneRenderer */

var RalphDetailPanel = (function () {
  'use strict';

  var PANEL_WIDTH = '440px';

  // ── Constructor ───────────────────────────────────────────────────────

  function DetailPanel(options) {
    this.opts = options || {};
    this.containerId = this.opts.containerId || 'scene-3d-container';
    this.panelId = this.opts.panelId || 'ralph-detail-panel';
    this.actionList = [];
    this.currentIndex = -1;
    this.panel = null;
    this.overlay = null;
    this._isOpen = false;
    this._onKeyDown = this._handleKeyDown.bind(this);
  }

  // ── Initialization ────────────────────────────────────────────────────

  DetailPanel.prototype.init = function (actionList) {
    this.actionList = actionList || [];
    this._createElements();
    document.addEventListener('keydown', this._onKeyDown);
  };

  DetailPanel.prototype._createElements = function () {
    if (document.getElementById(this.panelId)) {
      this.panel = document.getElementById(this.panelId);
      return;
    }

    this.overlay = document.createElement('div');
    this.overlay.id = this.panelId + '-overlay';
    this.overlay.style.cssText =
      'position:fixed;top:0;left:0;width:100%;height:100%;' +
      'background:rgba(0,0,0,0.3);z-index:998;display:none;' +
      'transition:opacity 0.2s ease;opacity:0;';
    this.overlay.addEventListener('click', this.close.bind(this));
    document.body.appendChild(this.overlay);

    this.panel = document.createElement('div');
    this.panel.id = this.panelId;
    this.panel.style.cssText =
      'position:fixed;top:0;right:0;width:' + PANEL_WIDTH + ';max-width:90vw;height:100vh;' +
      'background:#161b22;border-left:1px solid #30363d;z-index:999;' +
      'transform:translateX(100%);transition:transform 0.3s cubic-bezier(0.4,0,0.2,1);' +
      'display:flex;flex-direction:column;overflow:hidden;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;' +
      'box-shadow:-4px 0 24px rgba(0,0,0,0.4);color:#e6edf3;';

    var header = document.createElement('div');
    header.style.cssText =
      'display:flex;align-items:center;gap:0.5rem;padding:0.75rem 1rem;' +
      'border-bottom:1px solid #30363d;background:#0d1117;flex-shrink:0;';

    var prevBtn = this._navButton('\u25C0', 'Previous action (Left arrow)', this.prev.bind(this));
    var nextBtn = this._navButton('\u25B6', 'Next action (Right arrow)', this.next.bind(this));

    var counter = document.createElement('span');
    counter.id = this.panelId + '-counter';
    counter.style.cssText = 'color:#8b949e;font-size:0.8rem;flex:1;text-align:center;';

    var closeBtn = document.createElement('button');
    closeBtn.innerHTML = '&times;';
    closeBtn.title = 'Close (Esc)';
    closeBtn.style.cssText =
      'background:none;border:none;color:#8b949e;font-size:1.4rem;cursor:pointer;' +
      'padding:0 0.3rem;line-height:1;';
    closeBtn.addEventListener('mouseover', function () { closeBtn.style.color = '#e6edf3'; });
    closeBtn.addEventListener('mouseout', function () { closeBtn.style.color = '#8b949e'; });
    closeBtn.addEventListener('click', this.close.bind(this));

    header.appendChild(prevBtn);
    header.appendChild(counter);
    header.appendChild(nextBtn);
    header.appendChild(closeBtn);

    var content = document.createElement('div');
    content.id = this.panelId + '-content';
    content.style.cssText = 'flex:1;overflow-y:auto;padding:1.25rem 1.5rem;';

    this.panel.appendChild(header);
    this.panel.appendChild(content);
    document.body.appendChild(this.panel);
  };

  DetailPanel.prototype._navButton = function (icon, title, handler) {
    var btn = document.createElement('button');
    btn.textContent = icon;
    btn.title = title;
    btn.style.cssText =
      'background:#21262d;border:1px solid #30363d;color:#e6edf3;' +
      'padding:0.25rem 0.55rem;border-radius:4px;cursor:pointer;font-size:0.85rem;';
    btn.addEventListener('mouseover', function () { btn.style.background = '#30363d'; });
    btn.addEventListener('mouseout', function () { btn.style.background = '#21262d'; });
    btn.addEventListener('click', handler);
    return btn;
  };

  // ── Open / Close ──────────────────────────────────────────────────────

  DetailPanel.prototype.open = function (userData) {
    if (!this.panel) return;

    if (userData && userData.id) {
      for (var i = 0; i < this.actionList.length; i++) {
        if (this.actionList[i].id === userData.id) {
          this.currentIndex = i;
          break;
        }
      }
    }

    this._renderContent(userData);
    this.panel.style.transform = 'translateX(0)';
    if (this.overlay) {
      this.overlay.style.display = 'block';
      var ov = this.overlay;
      requestAnimationFrame(function () { ov.style.opacity = '1'; });
    }
    this._isOpen = true;
    this._updateCounter();
  };

  DetailPanel.prototype.close = function () {
    if (!this.panel) return;
    this.panel.style.transform = 'translateX(100%)';
    if (this.overlay) {
      this.overlay.style.opacity = '0';
      var ov = this.overlay;
      setTimeout(function () { ov.style.display = 'none'; }, 200);
    }
    this._isOpen = false;
    this.currentIndex = -1;
  };

  DetailPanel.prototype.isOpen = function () {
    return this._isOpen;
  };

  // ── Navigation ────────────────────────────────────────────────────────

  DetailPanel.prototype.prev = function () {
    if (this.currentIndex <= 0 || this.actionList.length === 0) return;
    this.currentIndex--;
    this._renderFromList();
  };

  DetailPanel.prototype.next = function () {
    if (this.currentIndex >= this.actionList.length - 1) return;
    this.currentIndex++;
    this._renderFromList();
  };

  DetailPanel.prototype._renderFromList = function () {
    if (this.currentIndex < 0 || this.currentIndex >= this.actionList.length) return;
    this._renderContent(this.actionList[this.currentIndex]);
    this._updateCounter();
  };

  // ── Rendering ─────────────────────────────────────────────────────────

  DetailPanel.prototype._renderContent = function (userData) {
    var content = document.getElementById(this.panelId + '-content');
    if (!content) return;

    if (!userData) {
      content.innerHTML = '<p style="color:#8b949e;font-style:italic">No action selected.</p>';
      return;
    }

    var meta = userData.meta || {};
    var narrativeHtml = meta.narrative_html || '';
    var html = '';

    html += '<h3 style="margin-bottom:0.5rem;font-size:1.05rem">' +
      escapeHtml(userData.label || userData.actionType || 'Action') + '</h3>';

    html += '<div class="detail-status-bar">';
    html += '<span class="badge status-' + (userData.status || 'unknown') + '">' +
      (userData.status || '?').toUpperCase() + '</span>';
    html += '<span class="action-type action-type-' + (userData.actionType || 'default') + '">' +
      escapeHtml((userData.actionType || 'unknown').replace(/_/g, ' ')) + '</span>';
    if (userData.durationMs) {
      html += '<span style="color:#8b949e;font-size:0.8rem;margin-left:auto">' +
        formatDuration(userData.durationMs) + '</span>';
    }
    html += '</div>';

    if (narrativeHtml) {
      html += narrativeHtml;
    } else {
      html += renderFallbackContent(userData);
    }

    html += '<div class="detail-section" style="margin-top:0.75rem;padding-top:0.75rem;border-top:1px solid #30363d">';
    html += '<div class="detail-kv-list">';
    html += '<span class="kv-key">Phase</span><span class="kv-val">' + escapeHtml(userData.phase || '?') + '</span>';
    html += '<span class="kv-key">Iteration</span><span class="kv-val">' + (userData.iteration || 0) + '</span>';
    if (userData.timestamp) {
      html += '<span class="kv-key">Time</span><span class="kv-val">' + escapeHtml(userData.timestamp.substring(0, 19)) + '</span>';
    }
    if (userData.tokens) {
      html += '<span class="kv-key">Tokens</span><span class="kv-val">' + userData.tokens + '</span>';
    }
    html += '</div></div>';

    content.innerHTML = html;
  };

  DetailPanel.prototype._updateCounter = function () {
    var counter = document.getElementById(this.panelId + '-counter');
    if (!counter) return;
    if (this.actionList.length === 0) {
      counter.textContent = '';
    } else {
      counter.textContent = (this.currentIndex + 1) + ' / ' + this.actionList.length;
    }
  };

  // ── Keyboard ──────────────────────────────────────────────────────────

  DetailPanel.prototype._handleKeyDown = function (e) {
    if (!this._isOpen) return;
    if (e.key === 'Escape') { this.close(); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { this.prev(); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { this.next(); e.preventDefault(); }
  };

  // ── Cleanup ───────────────────────────────────────────────────────────

  DetailPanel.prototype.dispose = function () {
    document.removeEventListener('keydown', this._onKeyDown);
    if (this.overlay && this.overlay.parentNode) {
      this.overlay.parentNode.removeChild(this.overlay);
    }
    if (this.panel && this.panel.parentNode) {
      this.panel.parentNode.removeChild(this.panel);
    }
    this._isOpen = false;
  };

  // ── Fallback renderer (when no server-side narrative_html) ────────────

  function renderFallbackContent(userData) {
    var meta = userData.meta || {};
    var input = meta.input || {};
    var output = meta.output || {};
    var llm = meta.llm_context || {};
    var provenance = meta.provenance || {};
    var html = '';

    if (userData.actionType === 'llm_query') {
      html += '<div class="detail-section"><div class="detail-section-title">What the agent was told</div>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || 'No description available') + '</p></div>';

      if (provenance.reasoning) {
        html += '<div class="detail-section"><div class="detail-section-title">Key reasoning</div>';
        html += '<p class="detail-narrative">' + escapeHtml(provenance.reasoning) + '</p></div>';
      }

      if (llm.model || llm.provider) {
        html += '<div class="detail-section"><div class="detail-section-title">By the numbers</div>';
        html += '<div class="detail-kv-list">';
        if (llm.model) html += '<span class="kv-key">Model</span><span class="kv-val">' + escapeHtml(llm.model) + '</span>';
        if (llm.provider) html += '<span class="kv-key">Provider</span><span class="kv-val">' + escapeHtml(llm.provider) + '</span>';
        html += '<span class="kv-key">Tokens in</span><span class="kv-val">' + (llm.tokens_in || 0) + '</span>';
        html += '<span class="kv-key">Tokens out</span><span class="kv-val">' + (llm.tokens_out || 0) + '</span>';
        html += '</div></div>';
      }
    } else if (userData.actionType === 'file_read' || userData.actionType === 'file_write' || userData.actionType === 'file_search') {
      var fpath = input.path || (input.context && input.context.path) || '';
      html += '<div class="detail-section"><div class="detail-section-title">' +
        (userData.actionType === 'file_write' ? 'What was written' : 'What was read') + '</div>';
      if (fpath) html += '<p><span class="detail-file-path">' + escapeHtml(fpath) + '</span></p>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || '') + '</p></div>';
    } else {
      html += '<div class="detail-section"><div class="detail-section-title">What was run</div>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || 'No description') + '</p></div>';

      var stdout = output.stdout || (output.data && output.data.stdout) || '';
      var stderr = output.stderr || (output.data && output.data.stderr) || '';
      if (stdout || stderr) {
        html += '<div class="detail-section"><div class="detail-section-title">What happened</div>';
        html += '<div class="detail-code-block">';
        if (stdout) html += escapeHtml(stdout.substring(0, 1500));
        if (stderr) html += (stdout ? '\n---\n' : '') + escapeHtml(stderr.substring(0, 1500));
        html += '</div></div>';
      }
    }

    return html;
  }

  // ── Helpers ───────────────────────────────────────────────────────────

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function formatDuration(ms) {
    if (ms < 1000) return Math.round(ms) + 'ms';
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    return (s / 60).toFixed(1) + 'm';
  }

  /**
   * Build a flat action list from scene data for panel navigation.
   * Each item has the same shape as Three.js userData.
   */
  function buildActionList(sceneData) {
    var list = [];
    var platforms = sceneData.platforms || [];
    for (var i = 0; i < platforms.length; i++) {
      var objects = platforms[i].objects || [];
      for (var j = 0; j < objects.length; j++) {
        var obj = objects[j];
        list.push({
          id: obj.id,
          label: obj.label,
          actionType: obj.action_type,
          status: obj.status,
          phase: obj.phase,
          iteration: obj.iteration,
          tokens: obj.tokens,
          durationMs: obj.duration_ms,
          timestamp: obj.timestamp,
          meta: obj.meta || {},
        });
      }
    }
    return list;
  }

  // ── Public API ────────────────────────────────────────────────────────

  return {
    DetailPanel: DetailPanel,
    buildActionList: buildActionList,
    _internals: {
      escapeHtml: escapeHtml,
      formatDuration: formatDuration,
      renderFallbackContent: renderFallbackContent,
    },
  };
})();
