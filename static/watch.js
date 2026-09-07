(function () {
  const body = document.body;
  const videoId = body.dataset.videoId;
  const isLive = body.dataset.isLive === "true";
  const hlsUrl = body.dataset.hlsUrl || "";

  const isIOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  if (!videoId || isLive) {
    if (hlsUrl) initHls(document.getElementById("player"), hlsUrl);
    return;
  }

  const STORAGE_KEY = "kidtube:watch:" + videoId;
  const MIN_RESUME = 5;
  const END_BUFFER = 30;
  const SAVE_INTERVAL_MS = isIOS ? 250 : 500;
  const RESUME_BACKUP_SECONDS = 5;

  function loadProgress() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const time = parseFloat(raw);
      return Number.isFinite(time) && time > 0 ? time : null;
    } catch {
      return null;
    }
  }

  function saveProgress(time, duration) {
    if (time < MIN_RESUME) return;
    if (Number.isFinite(duration) && duration > 0 && time >= duration - END_BUFFER) {
      clearProgress();
      return;
    }
    try {
      localStorage.setItem(STORAGE_KEY, String(time));
    } catch {}
  }

  function clearProgress() {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {}
  }

  function resumeTarget(saved, duration) {
    const backed = saved - RESUME_BACKUP_SECONDS;
    if (backed < MIN_RESUME) return saved;
    if (Number.isFinite(duration) && duration > 0) {
      return Math.min(backed, duration - END_BUFFER);
    }
    return backed;
  }

  function roundSeekTime(time) {
    return Math.round(time * 10) / 10;
  }

  function isTimeSeekable(video, time) {
    const seekable = video.seekable;
    for (let i = 0; i < seekable.length; i++) {
      if (time >= seekable.start(i) && time <= seekable.end(i)) {
        return true;
      }
    }
    return false;
  }

  function seekVideo(video, time) {
    time = roundSeekTime(time);
    if (typeof video.fastSeek === "function") {
      try {
        video.fastSeek(time);
        return;
      } catch {}
    }
    video.currentTime = time;
  }

  function attachSaveHandlers(video, getLastPosition) {
    function flushSave() {
      saveProgress(getLastPosition(), video.duration);
    }

    let lastSave = 0;
    video.addEventListener("timeupdate", function () {
      const now = Date.now();
      if (now - lastSave < SAVE_INTERVAL_MS) return;
      lastSave = now;
      flushSave();
    });
    video.addEventListener("pause", flushSave);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flushSave();
    });
    window.addEventListener("pagehide", flushSave);

    document.addEventListener(
      "touchstart",
      function (event) {
        if (event.target.closest("a")) flushSave();
      },
      true
    );
    document.addEventListener(
      "click",
      function (event) {
        if (event.target.closest("a")) flushSave();
      },
      true
    );
  }

  function setupVideoDesktop(video, saved) {
    let started = false;
    let lastPosition = 0;

    function flushSave() {
      saveProgress(lastPosition, video.duration);
    }

    if (saved !== null && saved >= MIN_RESUME) {
      video.autoplay = false;
      video.removeAttribute("autoplay");
    } else {
      video.autoplay = true;
    }

    function startFresh() {
      if (started) return;
      started = true;
      video.play().catch(function () {});
    }

    function tryResume() {
      if (started) return;

      if (saved === null || saved < MIN_RESUME) {
        startFresh();
        return;
      }

      const duration = video.duration;
      if (!Number.isFinite(duration) || duration <= 0) return;

      if (saved >= duration - END_BUFFER) {
        clearProgress();
        startFresh();
        return;
      }

      video.pause();
      seekVideo(video, resumeTarget(saved, duration));
    }

    video.addEventListener("seeked", function () {
      if (saved !== null && saved >= MIN_RESUME && !started) {
        started = true;
        video.play().catch(function () {});
      }
    });

    video.addEventListener("loadedmetadata", tryResume);
    video.addEventListener("canplay", tryResume);
    video.addEventListener("ended", clearProgress);

    if (video.readyState >= 1) {
      tryResume();
    }

    video.addEventListener("timeupdate", function () {
      lastPosition = video.currentTime;
    });
    attachSaveHandlers(video, function () {
      return lastPosition;
    });
  }

  function setupVideoIOS(video, saved) {
    let lastPosition = 0;
    let resumeDone = false;
    let target = saved !== null && saved >= MIN_RESUME ? resumeTarget(saved, 0) : null;

    const source = video.querySelector("source");
    if (source && target !== null) {
      const base = source.src.split("#")[0];
      source.src = base + "#t=" + Math.floor(target);
      video.load();
      video.autoplay = false;
      video.removeAttribute("autoplay");
    } else {
      video.autoplay = true;
    }

    function atTarget() {
      if (target === null) return true;
      return Math.abs(video.currentTime - target) <= 2;
    }

    function beginPlayback() {
      if (resumeDone) return;
      resumeDone = true;
      video.play().catch(function () {});
    }

    function tryIOSSeek() {
      if (target === null) {
        beginPlayback();
        return;
      }

      const duration = video.duration;
      if (Number.isFinite(duration) && duration > 0 && saved >= duration - END_BUFFER) {
        clearProgress();
        target = null;
        beginPlayback();
        return;
      }

      if (atTarget()) {
        beginPlayback();
        return;
      }

      if (isTimeSeekable(video, target) || video.readyState >= 2) {
        video.pause();
        seekVideo(video, target);
        return;
      }
    }

    // WebKit bug 261512: do not seek on loadedmetadata — use loadeddata/progress instead.
    ["loadeddata", "canplay", "progress"].forEach(function (eventName) {
      video.addEventListener(eventName, tryIOSSeek);
    });

    video.addEventListener("seeked", function () {
      if (target !== null && atTarget()) {
        beginPlayback();
      }
    });

    // Plyr workaround: iOS sometimes only accepts a seek after playback starts.
    video.addEventListener(
      "playing",
      function onPlaying() {
        if (target !== null && !atTarget()) {
          seekVideo(video, target);
        } else if (!resumeDone) {
          beginPlayback();
        }
        video.removeEventListener("playing", onPlaying);
      },
      { once: true }
    );

    let pollCount = 0;
    const poll = window.setInterval(function () {
      pollCount += 1;
      if (resumeDone) {
        window.clearInterval(poll);
        return;
      }
      if (pollCount > 40) {
        window.clearInterval(poll);
        if (target !== null && !atTarget()) {
          seekVideo(video, target);
        }
        beginPlayback();
        return;
      }
      tryIOSSeek();
    }, 250);

    video.addEventListener("ended", clearProgress);
    video.addEventListener("timeupdate", function () {
      lastPosition = video.currentTime;
    });
    attachSaveHandlers(video, function () {
      return lastPosition;
    });

    if (target === null) {
      beginPlayback();
    }
  }

  function setupVideo(video) {
    const saved = loadProgress();
    if (isIOS) {
      setupVideoIOS(video, saved);
    } else {
      setupVideoDesktop(video, saved);
    }
  }

  function setupIframe(iframe) {
    const saved = loadProgress();
    if (saved !== null && saved >= MIN_RESUME) {
      const url = new URL(iframe.src);
      const start = Math.max(0, Math.floor(saved - RESUME_BACKUP_SECONDS));
      url.searchParams.set("start", String(start));
      iframe.src = url.toString();
    }
  }

  function initHls(video, src) {
    if (!video || !src) return;

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src;
      return;
    }
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(src);
      hls.attachMedia(video);
      hls.on(Hls.Events.ERROR, function (_, data) {
        if (data.fatal && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          hls.loadSource(src);
          hls.startLoad();
        }
      });
      return;
    }
    video.outerHTML =
      '<div class="player-error"><p>Live stream playback is not supported in this browser.</p></div>';
  }

  const video = document.getElementById("player");
  const iframe = document.getElementById("player-embed");

  if (video) {
    setupVideo(video);
    if (hlsUrl) initHls(video, hlsUrl);
  } else if (iframe) {
    setupIframe(iframe);
  }
})();
