import Link from "next/link";
import { ArrowRight, Bell, TrendingUp, UsersRound } from "lucide-react";
import { PredictiveArcSignal } from "./PredictiveArcSignal";

const features = [
  {
    title: "Segmentation",
    description: "Find and prioritize the most relevant customers.",
    icon: UsersRound,
  },
  {
    title: "Forecasting",
    description: "Understand future calibration activity and expected volume.",
    icon: TrendingUp,
  },
  {
    title: "Signals",
    description: "Spot unusual inactivity and services worth discussing.",
    icon: Bell,
  },
];

export function LandingPage() {
  return (
    <div className="landing-shell">
      <div className="landing-background" aria-hidden="true">
        <PredictiveArcSignal
          background="#f5f8fe"
          baseColor="#b7c6dc"
          accentColor="#6888b9"
          highlight="#749ce8"
          density={130}
          dotSize={90}
          speed={22}
          hover={0}
          signal={{ level: 52, amplitude: 23, thickness: 130, wavelength: 85 }}
        />
      </div>
      <header className="landing-header landing-container">
        <Link
          href="/"
          className="landing-brand"
          aria-label="SignalPilot home"
        >
          Signal<span className="landing-brand-accent">Pilot</span>
        </Link>
      </header>
      <main>
        <section className="landing-hero" aria-labelledby="landing-heading">
          <div className="landing-container landing-hero-inner">
            <div className="landing-copy">
              <p className="landing-eyebrow">Introducing SignalPilot</p>
              <h1 id="landing-heading">
                From customer signals to <span className="landing-accent">trusted action.</span>
              </h1>
              <p className="landing-description" id="about">
                An agentic AI platform for customer intelligence, risk detection
                and secure workflows.
              </p>
              <p className="landing-stack">AGENTS · GEMINI · PULSEGUARD · N8N</p>
              <div className="landing-actions">
                <Link
                  href="/dashboard"
                  className="landing-button landing-button-primary"
                >
                  Open Dashboard <ArrowRight size={23} aria-hidden="true" />
                </Link>
                <Link href="/pulse" className="landing-button">
                  Open Agent Ops
                </Link>
              </div>
            </div>
          </div>
        </section>
        <section
          className="landing-features landing-container"
          id="product"
          aria-labelledby="landing-features-heading"
        >
          <p className="landing-eyebrow">Key features</p>
          <h2 id="landing-features-heading">
            Turn data into your next best conversation
          </h2>
          <div className="landing-feature-grid">
            {features.map(({ title, description, icon: Icon }) => (
              <article className="landing-feature-card" key={title}>
                <span
                  className={`landing-feature-icon${title === "Signals" ? " landing-feature-icon-orange" : ""}`}
                >
                  <Icon size={32} strokeWidth={2.2} aria-hidden="true" />
                </span>
                <div>
                  <h3>{title}</h3>
                  <p>{description}</p>
                </div>
              </article>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
