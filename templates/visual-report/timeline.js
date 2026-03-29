/**
 * Timeline Scrubber for the 3D Execution Landscape.
 *
 * Horizontal bar at the bottom of the 3D viewport showing wall-clock
 * execution time.  Features:
 *   - Phase-colored marker segments for quick visual orientation
 *   - Draggable scrubber thumb to scrub through execution time
 *   - Play/pause button with configurable speed (1x, 2x, 5x, 10x)
 *   - Click-to-jump on the timeline bar
 *   - Current-time display
 *   - Scene synchronization: objects appear/highlight chronologically
 *   - Clicking a 3D object snaps the scrubber to that action's timestamp
 *
 * Reads TimelineData JSON produced by engine/visualization/scene/timeline.py.
 */

/* global RalphSceneRenderer */

var RalphTimeline = (function () {
  'use strict';

  var SPEEDS = [1, 2, 5, 10];
  var BAR_HEIGHT = 6;
  var THUMB_SIZE = 14;

  // ── Constructor ───────────────────────────────────────────────────────

  function Timeline(timelineData, options) {
    this.data = timelineData || {};
    this.opts = options || {};
    this.containerId = this.opts.containerId || 'scene-3d-container';
    this.totalDuration = this.data.total_duration_ms || 0;
    this.markers = this.data.markers || [];
    this.events = this.data.events || [];

    this.currentTime = 0;
    this.isPlaying = false;
    this.speedIndex = 0;
    this._animationId = null;
    this._lastFrameTime = 0;
    this._disposed = false;

    this._onObjectClick = this.opts.onObjectClick || null;
    this._onTimeChange = this.opts.onTimeChange || null;

    this.el = null;
    this._bar = null;
    this._thumb = null;
    this._playBtn = null;
    this._speedBtn = null;
    this._timeDisplay = null;
    this._markerEls = [];
    this._eventDots = [];
    this._isDragging = false;

    this._boundMouseMove = this._onMouseMove.bind(this);
    this._boundMouseUp = this._onMouseUp.bind(this);
  }

  // ── Initialization ────────────────────────────────────────────────────

  Timeline.prototype.init = function () {
    if (this.totalDuration <= 0) return false;

    var container = document.getElementById(this.containerId);
    if (!container) return false;
    container.style.position = 'relative';

    this._createDOM(container);
    this._renderMarkers();
    this._renderEventDots();
    this._updateThumb();
    this._updateTimeDisplay();
    return true;
  };

  Timeline.prototype._createDOM = function (container) {
    this.el = document.createElement('div');
    this.el.id = 'ralph-timeline';
    this.el.style.cssText =
      'position:absolute;bottom:0;left:0;right:0;' +
      'background:rgba(13,17,23,0.92);border-top:1px solid #30363d;' +
      'padding:8px 16px 10px;z-index:20;display:flex;align-items:center;gap:10px;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;' +
      'user-select:none;-webkit-user-select:none;';

    this._playBtn = document.createElement('button');
    this._playBtn.textContent = '\u25B6';
    this._playBtn.title = 'Play / Pause';
    this._playBtn.style.cssText =
      'background:#21262d;border:1px solid #30363d;color:#e6edf3;' +
      'width:30px;height:26px;border-radius:4px;cursor:pointer;font-size:0.8rem;' +
      'display:flex;align-items:center;justify-content:center;flex-shrink:0;';
    this._playBtn.addEventListener('click', this.togglePlay.bind(this));

    this._speedBtn = document.createElement('button');
    this._speedBtn.textContent = '1x';
    this._speedBtn.title = 'Playback speed';
    this._speedBtn.style.cssText =
      'background:#21262d;border:1px solid #30363d;color:#8b949e;' +
      'padding:2px 6px;border-radius:4px;cursor:pointer;font-size:0.7rem;' +
      'min-width:28px;flex-shrink:0;';
    this._speedBtn.addEventListener('click', this.cycleSpeed.bind(this));

    var trackWrap = document.createElement('div');
    trackWrap.style.cssText = 'flex:1;position:relative;height:20px;display:flex;align-items:center;';

    this._bar = document.createElement('div');
    this._bar.style.cssText =
      'position:absolute;left:0;right:0;height:' + BAR_HEIGHT + 'px;' +
      'background:#21262d;border-radius:' + (BAR_HEIGHT / 2) + 'px;cursor:pointer;overflow:hidden;';
    this._bar.addEventListener('click', this._onBarClick.bind(this));

    this._thumb = document.createElement('div');
    this._thumb.style.cssText =
      'position:absolute;top:50%;width:' + THUMB_SIZE + 'px;height:' + THUMB_SIZE + 'px;' +
      'background:#58a6ff;border:2px solid #e6edf3;border-radius:50%;cursor:grab;' +
      'transform:translate(-50%,-50%);z-index:2;transition:box-shadow 0.15s;';
    this._thumb.addEventListener('mousedown', this._onThumbDown.bind(this));

    trackWrap.appendChild(this._bar);
    trackWrap.appendChild(this._thumb);

    this._timeDisplay = document.createElement('span');
    this._timeDisplay.style.cssText =
      'color:#8b949e;font-size:0.75rem;min-width:80px;text-align:right;flex-shrink:0;' +
      'font-variant-numeric:tabular-nums;';

    this.el.appendChild(this._playBtn);
    this.el.appendChild(this._speedBtn);
    this.el.appendChild(trackWrap);
    this.el.appendChild(this._timeDisplay);
    container.appendChild(this.el);
  };

  // ── Phase Markers ─────────────────────────────────────────────────────

  Timeline.prototype._renderMarkers = function () {
    if (!this._bar || this.totalDuration <= 0) return;

    for (var i = 0; i < this.markers.length; i++) {
      var m = this.markers[i];
      var leftPct = (m.start_ms / this.totalDuration) * 100;
      var widthPct = ((m.end_ms - m.start_ms) / this.totalDuration) * 100;
      widthPct = Math.max(widthPct, 0.5);

      var seg = document.createElement('div');
      seg.style.cssText =
        'position:absolute;top:0;height:100%;border-radius:' + (BAR_HEIGHT / 2) + 'px;' +
        'background:' + m.color + ';opacity:0.6;pointer-events:none;' +
        'left:' + leftPct + '%;width:' + widthPct + '%;';
      seg.title = m.label + ' (' + formatDuration(m.start_ms) + ' \u2013 ' + formatDuration(m.end_ms) + ')';
      this._bar.appendChild(seg);
      this._markerEls.push(seg);
    }
  };

  // ── Event Dots ────────────────────────────────────────────────────────

  Timeline.prototype._renderEventDots = function () {
    var trackWrap = this._bar ? this._bar.parentNode : null;
    if (!trackWrap || this.totalDuration <= 0) return;

    for (var i = 0; i < this.events.length; i++) {
      var ev = this.events[i];
      var leftPct = (ev.timestamp_ms / this.totalDuration) * 100;

      var dot = document.createElement('div');
      dot.style.cssText =
        'position:absolute;top:50%;width:4px;height:4px;border-radius:50%;' +
        'background:#e6edf3;opacity:0.4;pointer-events:none;z-index:1;' +
        'transform:translate(-50%,-50%);left:' + leftPct + '%;';
      dot.dataset.eventId = ev.id;
      trackWrap.appendChild(dot);
      this._eventDots.push(dot);
    }
  };

  // ── Play / Pause ──────────────────────────────────────────────────────

  Timeline.prototype.togglePlay = function () {
    if (this.isPlaying) {
      this.pause();
    } else {
      this.play();
    }
  };

  Timeline.prototype.play = function () {
    if (this.totalDuration <= 0) return;
    if (this.currentTime >= this.totalDuration) {
      this.currentTime = 0;
    }
    this.isPlaying = true;
    this._playBtn.textContent = '\u23F8';
    this._lastFrameTime = performance.now();
    this._tick();
  };

  Timeline.prototype.pause = function () {
    this.isPlaying = false;
    this._playBtn.textContent = '\u25B6';
    if (this._animationId) {
      cancelAnimationFrame(this._animationId);
      this._animationId = null;
    }
  };

  Timeline.prototype._tick = function () {
    if (!this.isPlaying || this._disposed) return;

    var now = performance.now();
    var delta = now - this._lastFrameTime;
    this._lastFrameTime = now;

    var speed = SPEEDS[this.speedIndex] || 1;
    this.currentTime += delta * speed;

    if (this.currentTime >= this.totalDuration) {
      this.currentTime = this.totalDuration;
      this.pause();
    }

    this._updateThumb();
    this._updateTimeDisplay();
    this._highlightVisibleEvents();
    if (this._onTimeChange) this._onTimeChange(this.currentTime);

    if (this.isPlaying) {
      this._animationId = requestAnimationFrame(this._tick.bind(this));
    }
  };

  Timeline.prototype.cycleSpeed = function () {
    this.speedIndex = (this.speedIndex + 1) % SPEEDS.length;
    this._speedBtn.textContent = SPEEDS[this.speedIndex] + 'x';
  };

  // ── Scrubbing ─────────────────────────────────────────────────────────

  Timeline.prototype.seekTo = function (timeMs) {
    this.currentTime = Math.max(0, Math.min(timeMs, this.totalDuration));
    this._updateThumb();
    this._updateTimeDisplay();
    this._highlightVisibleEvents();
    if (this._onTimeChange) this._onTimeChange(this.currentTime);
  };

  Timeline.prototype.seekToEvent = function (eventId) {
    for (var i = 0; i < this.events.length; i++) {
      if (this.events[i].id === eventId) {
        this.seekTo(this.events[i].timestamp_ms);
        return true;
      }
    }
    return false;
  };

  Timeline.prototype.seekToPhase = function (phase) {
    for (var i = 0; i < this.markers.length; i++) {
      if (this.markers[i].phase === phase) {
        this.seekTo(this.markers[i].start_ms);
        return true;
      }
    }
    return false;
  };

  // ── Thumb Dragging ────────────────────────────────────────────────────

  Timeline.prototype._onThumbDown = function (e) {
    e.preventDefault();
    this._isDragging = true;
    this._thumb.style.cursor = 'grabbing';
    this._thumb.style.boxShadow = '0 0 0 3px rgba(88,166,255,0.3)';
    if (this.isPlaying) this.pause();
    document.addEventListener('mousemove', this._boundMouseMove);
    document.addEventListener('mouseup', this._boundMouseUp);
  };

  Timeline.prototype._onMouseMove = function (e) {
    if (!this._isDragging) return;
    this._seekFromPageX(e.pageX);
  };

  Timeline.prototype._onMouseUp = function () {
    this._isDragging = false;
    this._thumb.style.cursor = 'grab';
    this._thumb.style.boxShadow = '';
    document.removeEventListener('mousemove', this._boundMouseMove);
    document.removeEventListener('mouseup', this._boundMouseUp);
  };

  Timeline.prototype._onBarClick = function (e) {
    this._seekFromPageX(e.pageX);
  };

  Timeline.prototype._seekFromPageX = function (pageX) {
    var rect = this._bar.getBoundingClientRect();
    var fraction = (pageX - rect.left) / rect.width;
    fraction = Math.max(0, Math.min(1, fraction));
    this.seekTo(fraction * this.totalDuration);
  };

  // ── Visual Updates ────────────────────────────────────────────────────

  Timeline.prototype._updateThumb = function () {
    if (!this._thumb || this.totalDuration <= 0) return;
    var pct = (this.currentTime / this.totalDuration) * 100;
    this._thumb.style.left = pct + '%';
  };

  Timeline.prototype._updateTimeDisplay = function () {
    if (!this._timeDisplay) return;
    this._timeDisplay.textContent =
      formatDuration(this.currentTime) + ' / ' + formatDuration(this.totalDuration);
  };

  Timeline.prototype._highlightVisibleEvents = function () {
    for (var i = 0; i < this._eventDots.length; i++) {
      var ev = this.events[i];
      if (!ev) continue;
      var visible = ev.timestamp_ms <= this.currentTime;
      this._eventDots[i].style.opacity = visible ? '0.9' : '0.25';
      this._eventDots[i].style.width = visible ? '5px' : '4px';
      this._eventDots[i].style.height = visible ? '5px' : '4px';
    }
  };

  // ── Cleanup ───────────────────────────────────────────────────────────

  Timeline.prototype.dispose = function () {
    this._disposed = true;
    this.pause();
    document.removeEventListener('mousemove', this._boundMouseMove);
    document.removeEventListener('mouseup', this._boundMouseUp);
    if (this.el && this.el.parentNode) {
      this.el.parentNode.removeChild(this.el);
    }
  };

  // ── Helpers ───────────────────────────────────────────────────────────

  function formatDuration(ms) {
    if (ms < 1000) return Math.round(ms) + 'ms';
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    var m = Math.floor(s / 60);
    var rs = Math.floor(s % 60);
    return m + ':' + (rs < 10 ? '0' : '') + rs;
  }

  // ── Public API ────────────────────────────────────────────────────────

  return {
    Timeline: Timeline,
    SPEEDS: SPEEDS,
    _internals: {
      formatDuration: formatDuration,
    },
  };
})();

/**
 * Entry point: renderTimeline(timelineData, containerId, options)
 * Called from report HTML after timeline data is loaded.
 */
function renderTimeline(timelineData, containerId, options) {
  if (!timelineData || !timelineData.total_duration_ms) return null;
  var opts = options || {};
  opts.containerId = containerId || 'scene-3d-container';
  var tl = new RalphTimeline.Timeline(timelineData, opts);
  if (tl.init()) return tl;
  return null;
}
