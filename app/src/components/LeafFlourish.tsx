import { useCallback, useEffect, useRef, useState } from "react";

const DURATION_MS = 1600;
const COLORS = ["#accfa4", "#89a08f", "#c7dcb4", "#e8b3c3"];

/** A brief, decorative leaf fan after a successful action. It never captures
 * input or announces itself, and its lifetime survives ordinary re-renders. */
function LeafFlourish({ onDone }: { onDone: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (motion.matches || !canvas || !ctx) {
      const timer = setTimeout(() => doneRef.current(), 0);
      return () => clearTimeout(timer);
    }
    let w = 0;
    let h = 0;
    let scale = 0;
    const resize = () => {
      const nextScale = window.devicePixelRatio || 1;
      if (
        w === canvas.offsetWidth &&
        h === canvas.offsetHeight &&
        scale === nextScale
      )
        return;
      w = canvas.offsetWidth;
      h = canvas.offsetHeight;
      scale = nextScale;
      canvas.width = w * scale;
      canvas.height = h * scale;
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
    };
    resize();
    const leaves = Array.from({ length: 18 }, (_, i) => ({
      spread: (i / 17 - 0.5) * 2,
      size: 7 + Math.random() * 5,
      angle: Math.random() * Math.PI,
      turn: (Math.random() - 0.5) * 3,
      delay: Math.random() * 0.16,
      color: COLORS[i % COLORS.length],
    }));
    const started = performance.now();
    let frame = 0;
    const draw = (now: number) => {
      // Native form switches resize the viewport after the action completes.
      resize();
      const elapsed = (now - started) / DURATION_MS;
      ctx.clearRect(0, 0, w, h);
      for (const leaf of leaves) {
        const p = Math.max(
          0,
          Math.min(1, (elapsed - leaf.delay) / (1 - leaf.delay)),
        );
        if (p === 0 || p === 1) continue;
        const travel = 1 - (1 - p) ** 3;
        const x = w / 2 + leaf.spread * Math.min(150, w * 0.4) * travel;
        const y = h * 0.66 - Math.sin(p * Math.PI) * Math.min(100, h * 0.42);
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(leaf.angle + p * leaf.turn);
        ctx.scale(1, 0.75 + Math.sin(p * Math.PI * 2) * 0.2);
        ctx.globalAlpha = Math.min(1, p * 12) * (1 - p) ** 0.7;
        ctx.fillStyle = leaf.color;
        const n = leaf.size;
        ctx.beginPath();
        ctx.moveTo(-n, 0);
        ctx.bezierCurveTo(-n * 0.2, -n * 0.9, n * 0.7, -n * 0.65, n, 0);
        ctx.bezierCurveTo(n * 0.2, n * 0.9, -n * 0.7, n * 0.65, -n, 0);
        ctx.fill();
        ctx.strokeStyle = "#38583d";
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.moveTo(-n, 0);
        ctx.quadraticCurveTo(0, -n * 0.15, n * 0.8, 0);
        ctx.stroke();
        ctx.restore();
      }
      if (elapsed < 1) frame = requestAnimationFrame(draw);
      else doneRef.current();
    };
    frame = requestAnimationFrame(draw);
    const reduceMotion = () => {
      if (motion.matches) doneRef.current();
    };
    motion.addEventListener("change", reduceMotion);
    return () => {
      cancelAnimationFrame(frame);
      motion.removeEventListener("change", reduceMotion);
    };
  }, []);

  return (
    <canvas ref={canvasRef} className="leaf-flourish" aria-hidden="true" />
  );
}

export function useLeafFlourish() {
  const [burst, setBurst] = useState(0);
  const flourish = useCallback(() => setBurst((value) => value + 1), []);
  const finish = useCallback(() => setBurst(0), []);
  return {
    flourish,
    leaves: burst > 0 ? <LeafFlourish key={burst} onDone={finish} /> : null,
  };
}
