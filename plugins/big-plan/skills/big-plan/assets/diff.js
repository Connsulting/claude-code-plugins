// big-plan diff view: expand collapsed unchanged regions and accept hunks
// into the baseline one at a time ("reviewed").
(function () {
  const relpath = document.body.dataset.relpath;

  function encodeURIPath(p) {
    return p.split("/").map(encodeURIComponent).join("/");
  }

  // -- Expand collapsed unchanged regions --
  document.querySelectorAll(".diff-expand").forEach((btn) => {
    btn.addEventListener("click", () => {
      const lines = btn.parentElement.querySelector(".diff-gap-lines");
      if (!lines) return;
      lines.hidden = !lines.hidden;
      btn.classList.toggle("expanded", !lines.hidden);
    });
  });

  if (!relpath) return;

  // -- Accept one hunk into the baseline --
  document.querySelectorAll(".hunk-reviewed").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const body = {
        old_start: Number(btn.dataset.oldStart),
        old_end: Number(btn.dataset.oldEnd),
        old_b64: btn.dataset.oldB64,
        new_b64: btn.dataset.newB64,
      };
      try {
        const res = await fetch(`/api/snapshot/${encodeURIPath(relpath)}/accept`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.ok) {
          // Drop the hunk from view; reload to recompute remaining hunks and
          // their (now-shifted) line ranges from the updated baseline.
          window.location.reload();
        } else {
          const msg = await res.json().catch(() => ({}));
          alert("Could not accept: " + (msg.error || res.status) + "\nReloading.");
          window.location.reload();
        }
      } catch (e) {
        btn.disabled = false;
        alert("Could not accept: " + e.message);
      }
    });
  });
})();
