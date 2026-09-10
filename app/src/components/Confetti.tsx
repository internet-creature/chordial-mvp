import { useEffect, useRef } from "react";

const COLORS = ["#accfa4", "#89a08f", "#e8b3c3", "#dab58c", "#e5ede3"];
const COUNT = 140;
const DURATION_MS = 2600;

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  size: number;
  color: string;
  spin: number;
  angle: number;
}

/** a self-contained canvas confetti burst - no libraries (the CSP has no
 * friends). fires once on mount, calls onDone when the last piece settles.
 * under prefers-reduced-motion it skips straight to onDone. */
export default function Confetti({ onDone }: { onDone: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const timer = setTimeout(onDone, 400);
      return () => clearTimeout(timer);
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.offsetWidth;
    const h = canvas.offsetHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);

    const particles: Particle[] = Array.from({ length: COUNT }, () => {
      const angle = -Math.PI / 2 + (Math.random() - 0.5) * 1.6;
      const speed = 4 + Math.random() * 7;
      return {
        x: w / 2 + (Math.random() - 0.5) * 40,
        y: h * 0.7,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        size: 4 + Math.random() * 5,
        color: COLORS[Math.floor(Math.random() * COLORS.length)],
        spin: (Math.random() - 0.5) * 0.3,
        angle: Math.random() * Math.PI,
      };
    });

    const started = performance.now();
    let frame = 0;

    const draw = (now: number) => {
      const t = now - started;
      ctx.clearRect(0, 0, w, h);
      const fade = Math.max(0, 1 - t / DURATION_MS);
      for (const p of particles) {
        p.vy += 0.18; // gravity
        p.vx *= 0.99;
        p.x += p.vx;
        p.y += p.vy;
        p.angle += p.spin;
        ctx.save();
        ctx.globalAlpha = fade;
        ctx.translate(p.x, p.y);
        ctx.rotate(p.angle);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size * 0.6);
        ctx.restore();
      }
      if (t < DURATION_MS) {
        frame = requestAnimationFrame(draw);
      } else {
        onDone();
      }
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, [onDone]);

  return <canvas ref={canvasRef} className="confetti" aria-hidden="true" />;
}
