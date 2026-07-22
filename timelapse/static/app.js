(() => {
  "use strict";

  document.body.addEventListener("htmx:beforeSwap", (event) => {
    const status = event.detail.xhr.status;
    if (status >= 400 && status < 600 && event.detail.xhr.responseText) {
      event.detail.shouldSwap = true;
    }
  });

  const form = document.querySelector("#export-form");
  const selectionCount = document.querySelector("#selection-count");
  const fullDayFields = document.querySelector("#full-day-fields");
  const exactFields = document.querySelector("#exact-fields");
  const previewButton = document.querySelector("#load-previews");
  const liveDot = document.querySelector("#live-dot");
  const liveLabel = document.querySelector("#live-label");
  const serverInfoButton = document.querySelector("#server-info-button");
  const serverInfoDialog = document.querySelector("#server-info-dialog");
  const previewTimers = { start: null, end: null };
  const previewControllers = { start: null, end: null };
  const previewDirty = { start: false, end: false };
  const previewDebounceMilliseconds = 350;
  let previewsActivated = false;

  function selectedCameras() {
    return Array.from(document.querySelectorAll('input[name="camera_ids"]:checked'));
  }

  function updateSelectionCount() {
    const count = selectedCameras().length;
    if (selectionCount) {
      selectionCount.textContent = `${count} selected`;
    }
  }

  function updateRangeMode() {
    const mode = form?.querySelector('input[name="range_mode"]:checked')?.value;
    if (fullDayFields && exactFields) {
      fullDayFields.hidden = mode !== "full-day";
      exactFields.hidden = mode !== "exact";
    }
  }

  function boundaryTimestamps() {
    const mode = form?.querySelector('input[name="range_mode"]:checked')?.value;
    if (mode === "exact") {
      return {
        start: form?.querySelector('input[name="start"]')?.value,
        end: form?.querySelector('input[name="end"]')?.value,
      };
    }
    const day = form?.querySelector('input[name="day"]')?.value;
    if (!day) {
      return { start: null, end: null };
    }
    const start = new Date(`${day}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    const localValue = (value) => {
      const offset = value.getTimezoneOffset();
      const local = new Date(value.getTime() - offset * 60_000);
      return local.toISOString().slice(0, 19);
    };
    return { start: localValue(start), end: localValue(end) };
  }

  async function loadPreview(card, cameraId, timestamp, controller) {
    const frame = card.querySelector(".preview-frame");
    const caption = card.querySelector("figcaption");
    const boundary = card.dataset.boundary;
    frame.classList.add("loading");
    try {
      const response = await fetch(
        `/api/thumbnails/${encodeURIComponent(cameraId)}?timestamp=${encodeURIComponent(timestamp)}`,
        { headers: { Accept: "image/jpeg" }, signal: controller.signal },
      );
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const blob = await response.blob();
      const oldImage = frame.querySelector("img");
      if (oldImage?.src.startsWith("blob:")) {
        URL.revokeObjectURL(oldImage.src);
      }
      const image = document.createElement("img");
      image.alt = `${card.dataset.boundary} camera preview`;
      image.src = URL.createObjectURL(blob);
      frame.replaceChildren(image);
      const source = response.headers.get("X-TimeLapse-Thumbnail-Source");
      caption.textContent = `${boundary === "start" ? "Start" : "End"} frame${source === "live" ? " · live fallback" : ""}`;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      const message = error instanceof Error ? error.message : "Preview unavailable";
      frame.replaceChildren(Object.assign(document.createElement("span"), { textContent: message }));
    } finally {
      if (previewControllers[boundary] === controller) {
        frame.classList.remove("loading");
        previewControllers[boundary] = null;
      }
    }
  }

  function loadPreviewBoundary(boundary) {
    const camera = selectedCameras()[0];
    const timestamps = boundaryTimestamps();
    const card = document.querySelector(`.preview-card[data-boundary="${boundary}"]`);
    const frame = card?.querySelector(".preview-frame");
    if (!card || !frame) return;

    previewControllers[boundary]?.abort();
    previewControllers[boundary] = null;
    if (!camera || !timestamps[boundary]) {
      frame.classList.remove("loading");
      frame.replaceChildren(Object.assign(document.createElement("span"), { textContent: "Select a camera and range" }));
      return;
    }

    const controller = new AbortController();
    previewControllers[boundary] = controller;
    void loadPreview(card, camera.value, timestamps[boundary], controller);
  }

  function schedulePreview(boundary) {
    if (!previewsActivated) return;
    clearTimeout(previewTimers[boundary]);
    previewTimers[boundary] = setTimeout(() => {
      previewTimers[boundary] = null;
      loadPreviewBoundary(boundary);
    }, previewDebounceMilliseconds);
  }

  function previewBoundariesForInput(target) {
    if (target.matches('input[name="start"]')) return ["start"];
    if (target.matches('input[name="end"]')) return ["end"];
    if (target.matches('input[name="day"]')) return ["start", "end"];
    return [];
  }

  function markPreviewDirty(target) {
    if (!previewsActivated) return;
    for (const boundary of previewBoundariesForInput(target)) {
      previewDirty[boundary] = true;
    }
  }

  function refreshDirtyPreviews(target) {
    for (const boundary of previewBoundariesForInput(target)) {
      if (!previewDirty[boundary]) continue;
      previewDirty[boundary] = false;
      schedulePreview(boundary);
    }
  }

  function loadPreviews() {
    previewsActivated = true;
    if (previewButton) previewButton.textContent = "Refresh previews";
    for (const boundary of ["start", "end"]) {
      clearTimeout(previewTimers[boundary]);
      previewTimers[boundary] = null;
      previewDirty[boundary] = false;
      loadPreviewBoundary(boundary);
    }
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches('input[name="camera_ids"]')) {
      updateSelectionCount();
    }
    if (event.target.matches('input[name="range_mode"]')) {
      updateRangeMode();
      schedulePreview("start");
      schedulePreview("end");
    }
    if (previewBoundariesForInput(event.target).length) {
      markPreviewDirty(event.target);
    }
  });

  document.addEventListener("input", (event) => {
    markPreviewDirty(event.target);
  });

  document.addEventListener("focusout", (event) => {
    const target = event.target;
    if (!previewBoundariesForInput(target).length) return;
    setTimeout(() => {
      if (document.activeElement === target) return;
      refreshDirtyPreviews(target);
    }, 0);
  });

  document.addEventListener("click", (event) => {
    if (event.target.matches(".dismiss-toast")) {
      event.target.closest(".toast")?.remove();
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target?.id === "camera-picker") {
      updateSelectionCount();
    }
  });

  previewButton?.addEventListener("click", loadPreviews);
  serverInfoButton?.addEventListener("click", () => serverInfoDialog?.showModal());
  serverInfoDialog?.addEventListener("click", (event) => {
    if (event.target === serverInfoDialog) serverInfoDialog.close();
  });
  updateRangeMode();
  updateSelectionCount();

  const events = new EventSource("/api/events");
  events.addEventListener("open", () => {
    liveDot?.classList.add("online");
    liveDot?.classList.remove("offline");
    if (liveLabel) liveLabel.textContent = "Server connected";
  });
  events.addEventListener("state", () => {
    if (window.htmx) window.htmx.trigger(document.body, "stateChanged");
  });
  events.addEventListener("error", () => {
    liveDot?.classList.remove("online");
    liveDot?.classList.add("offline");
    if (liveLabel) liveLabel.textContent = "Reconnecting";
  });
})();
