import React, { useState } from "react";

type ContentType =
  | "title"
  | "bullets"
  | "two_column"
  | "stat_callout"
  | "timeline"
  | "table"
  | "quote"
  | "diagram"
  | "thank_you";

interface SlideContent {
  title?: string;
  subtitle?: string;
  speaker_notes?: string;
  bullets?: string[];
  generated_image_path?: string;
}

interface Slide {
  slide_number: number;
  content_type: ContentType;
  content: SlideContent;
  layout_override?: string | null;
}

interface Theme {
  id: string;
  name?: string;
}

interface GenerateResponse {
  slides: Slide[];
  variants?: Slide[][];
  theme: Theme;
}

const API_BASE = "http://localhost:4001";

const DASHBOARD_THEMES = [
  { id: "midnight_executive", name: "Midnight Executive", colors: ["#1E2761", "#CADCFC", "#FFFFFF"] },
  { id: "coral_energy", name: "Coral Energy", colors: ["#F96167", "#F9E795", "#2F3C7E"] },
  { id: "ocean_gradient", name: "Ocean Gradient", colors: ["#065A82", "#1C7293", "#21295C"] },
  { id: "forest_moss", name: "Forest Moss", colors: ["#2C5F2D", "#97BC62", "#F5F5F5"] },
  { id: "charcoal_minimal", name: "Charcoal Minimal", colors: ["#36454F", "#F2F2F2", "#212121"] },
  { id: "warm_terracotta", name: "Warm Terracotta", colors: ["#B85042", "#E7E8D1", "#A7BEAE"] },
];

const MOCK_PRESENTATIONS = [
  { id: 1, title: "Q3 Marketing Strategy", date: "2 days ago", slides: 12, theme: "Midnight Executive", img: "https://images.unsplash.com/photo-1460925895917-afdab827c52f?auto=format&fit=crop&q=80&w=600" },
  { id: 2, title: "AI Ethics Deep Dive", date: "1 week ago", slides: 8, theme: "Ocean Gradient", img: "https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&q=80&w=600" },
  { id: 3, title: "Product Launch Roadmap", date: "2 weeks ago", slides: 15, theme: "Coral Energy", img: "https://images.unsplash.com/photo-1542744173-8e7e53415bb0?auto=format&fit=crop&q=80&w=600" },
];

const MOCK_LIBRARY = [
  { id: 1, name: "Premium Icons Pack", items: 250, category: "Assets", img: "https://images.unsplash.com/photo-1557683316-973673baf926?auto=format&fit=crop&q=80&w=400" },
  { id: 2, name: "Corporate Backgrounds", items: 50, category: "Images", img: "https://images.unsplash.com/photo-1497215728101-856f4ea42174?auto=format&fit=crop&q=80&w=400" },
  { id: 3, name: "Data Visualization Kits", items: 12, category: "Components", img: "https://images.unsplash.com/photo-1454165205744-3b78555e5572?auto=format&fit=crop&q=80&w=400" },
  { id: 4, name: "Abstract Gradients", items: 100, category: "Styles", img: "https://images.unsplash.com/photo-1550684848-fac1c5b4e853?auto=format&fit=crop&q=80&w=400" },
];

const DashboardIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" />
  </svg>
);

const App: React.FC = () => {
  const [prompt, setPrompt] = useState("");
  const [variants, setVariants] = useState<Slide[][] | null>(null);
  const [theme, setTheme] = useState<Theme | null>(null);
  const [selectedThemeId, setSelectedThemeId] = useState("midnight_executive");
  const [logicalSlideCount, setLogicalSlideCount] = useState(0);
  const [selectedSlideIndex, setSelectedSlideIndex] = useState(0);
  const [selectedVariantBySlide, setSelectedVariantBySlide] = useState<number[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"dashboard" | "editor" | "library" | "presentations">("dashboard");

  const generateAllVariants = async () => {
    if (!prompt.trim()) {
      setError("Please enter a topic or content.");
      return;
    }
    setError(null);
    setIsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: prompt, theme_id: selectedThemeId }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data: GenerateResponse = await res.json();
      const alignedVariants: Slide[][] = (data.variants && data.variants.length > 0 ? data.variants : [data.slides]);

      setLogicalSlideCount(data.slides.length);
      setTheme(data.theme);
      setSelectedVariantBySlide(new Array(data.slides.length).fill(0));
      setVariants(alignedVariants);
      setView("editor");
    } catch (e: any) {
      setError(e.message ?? "Failed to generate presentation.");
    } finally {
      setIsLoading(false);
    }
  };

  const handleExport = async () => {
    if (!variants || !variants[0] || !theme) return;
    setIsExporting(true);
    setError(null);
    try {
      const slidesForExport: Slide[] = [];
      for (let i = 0; i < logicalSlideCount; i++) {
        const chosenVariant = selectedVariantBySlide[i] ?? 0;
        const set = variants[chosenVariant] || variants[0];
        slidesForExport.push(set[i]);
      }
      const res = await fetch(`${API_BASE}/api/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slides: slidesForExport, theme }),
      });
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${prompt.slice(0, 20) || "presentation"}.pptx`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message ?? "Failed to compile PPTX.");
    } finally {
      setIsExporting(false);
    }
  };

  return (
    <div className="app-root">
      <aside className="sidebar">
        <div className="sidebar-logo">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" /></svg>
          Slide Forge
        </div>
        <nav className="nav-group">
          <div className={`nav-item ${view === 'dashboard' ? 'active' : ''}`} onClick={() => setView('dashboard')}>
            <span className="nav-item-icon"><DashboardIcon /></span> Dashboard
          </div>
          <div className={`nav-item ${view === 'presentations' ? 'active' : ''}`} onClick={() => setView('presentations')}>
            <span className="nav-item-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg></span> My Presentations
          </div>
          <div className={`nav-item ${view === 'library' ? 'active' : ''}`} onClick={() => setView('library')}>
            <span className="nav-item-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /></svg></span> Library
          </div>
          <div className="nav-item nav-item-add" onClick={() => { setVariants(null); setView('dashboard'); setError(null); setPrompt(""); }}>
            <span className="nav-item-icon">+</span> Generate New
          </div>
        </nav>
      </aside>

      <div className="main-wrapper">
        <header className="main-header">
          <div className="breadcrumb">
            Slide Forge / <span className="breadcrumb-active">{view.charAt(0).toUpperCase() + view.slice(1)}</span>
          </div>
          <div className="user-profile">
            <div className="avatar"></div>
          </div>
        </header>

        {view === 'dashboard' ? (
          <div className="dashboard-content">
            <div className="create-section">
              <div className="card start-ai-card">
                <div className="ai-icon-box">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
                </div>
                <h2 className="card-title">Forge with AI</h2>
                <div className="ai-input-wrapper">
                  <textarea
                    className="ai-textarea"
                    placeholder="Enter your topic: e.g. Future of renewable energy and sustainable tech"
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                  />
                </div>

                <h3 className="card-title" style={{ marginBottom: '1rem', fontSize: '1rem' }}>Choose Theme</h3>
                <div className="theme-selection-grid">
                  {DASHBOARD_THEMES.map((t) => (
                    <div
                      key={t.id}
                      className={`theme-card ${selectedThemeId === t.id ? 'theme-card-active' : ''}`}
                      onClick={() => setSelectedThemeId(t.id)}
                    >
                      <div className="theme-preview-colors">
                        {t.colors.map((c, i) => <div key={i} className="color-dot" style={{ background: c }}></div>)}
                      </div>
                      <div className="theme-name">{t.name}</div>
                    </div>
                  ))}
                </div>

                <div className="ai-options">
                  <div className="ai-option">Style: <span>Minimalist</span></div>
                  <div className="ai-option">Mode: <span>Professional</span></div>
                </div>
                <button className="btn-generate" onClick={generateAllVariants} disabled={isLoading}>
                  {isLoading ? "FORGING SLIDES..." : "FORGE PRESENTATION"}
                </button>
                {error && <div className="alert alert-error" style={{ marginTop: '1rem', color: '#ff4444' }}>{error}</div>}
              </div>
            </div>

            <div className="side-column">
              <div className="card">
                <div className="card-header">
                  <span className="card-title">AI Activity Feed</span>
                </div>
                <div className="activity-feed">
                  {MOCK_PRESENTATIONS.slice(0, 2).map((p, i) => (
                    <div key={p.id} className="activity-item" onClick={() => setView('presentations')} style={{ cursor: 'pointer' }}>
                      <div className="activity-icon">{i === 0 ? "✨" : "🎨"}</div>
                      <div className="activity-info">
                        <h4>Presentation "{p.title}" {i === 0 ? "generated" : "saved"}</h4>
                        <span>{p.date}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="card" onClick={() => setView('library')} style={{ cursor: 'pointer' }}>
                <div className="card-header">
                  <span className="card-title">My Library</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--accent-primary)', fontWeight: 600 }}>Manage →</span>
                </div>
                <div className="library-grid">
                  {MOCK_LIBRARY.slice(0, 4).map(item => (
                    <div
                      key={item.id}
                      className="library-preview"
                      style={{ backgroundImage: `url(${item.img})` }}
                      title={item.name}
                    ></div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        ) : view === 'presentations' ? (
          <div className="dashboard-content" style={{ gridTemplateColumns: '1fr' }}>
            <div className="presentations-list" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '2rem' }}>
              {MOCK_PRESENTATIONS.map(p => (
                <div key={p.id} className="card presentation-card" style={{ padding: 0, cursor: 'pointer' }} onClick={() => { setPrompt(p.title); setView('dashboard'); }}>
                  <div style={{ height: '160px', backgroundImage: `url(${p.img})`, backgroundSize: 'cover', backgroundPosition: 'center', borderRadius: '20px 20px 0 0' }}></div>
                  <div style={{ padding: '1.5rem' }}>
                    <h3 style={{ fontSize: '1.2rem', marginBottom: '0.5rem' }}>{p.title}</h3>
                    <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                      <span>{p.slides} slides</span>
                      <span>{p.date}</span>
                    </div>
                    <div style={{ marginTop: '1rem', paddingTop: '1rem', borderTop: '1px solid var(--glass-border)', color: 'var(--accent-primary)', fontSize: '0.8rem', fontWeight: 600 }}>
                      Theme: {p.theme}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : view === 'library' ? (
          <div className="dashboard-content" style={{ gridTemplateColumns: '1fr' }}>
            <div className="library-full-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '1.5rem' }}>
              {MOCK_LIBRARY.map(item => (
                <div key={item.id} className="card library-full-card" style={{ display: 'flex', gap: '1.5rem', alignItems: 'center', cursor: 'pointer' }} onClick={() => alert(`Opening ${item.name}...`)}>
                  <div style={{ width: '80px', height: '80px', borderRadius: '12px', backgroundImage: `url(${item.img})`, backgroundSize: 'cover', flexShrink: 0 }}></div>
                  <div>
                    <h3 style={{ fontSize: '1rem', marginBottom: '0.25rem' }}>{item.name}</h3>
                    <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{item.category} • {item.items} items</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : view === 'editor' ? (
          <EditorView
            variants={variants!}
            theme={theme}
            logicalSlideCount={logicalSlideCount}
            selectedSlideIndex={selectedSlideIndex}
            setSelectedSlideIndex={setSelectedSlideIndex}
            selectedVariantBySlide={selectedVariantBySlide}
            setSelectedVariantBySlide={setSelectedVariantBySlide}
            onExport={handleExport}
            isExporting={isExporting}
          />
        ) : (
          <div className="dashboard-content" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '400px' }}>
            <div className="card" style={{ textAlign: 'center', maxWidth: '400px' }}>
              <h2 className="card-title" style={{ marginBottom: '1rem' }}>No Items Found</h2>
              <p style={{ color: 'var(--text-muted)' }}>You haven't added anything to your {view} yet.</p>
              <button className="btn-generate" style={{ marginTop: '2rem' }} onClick={() => setView('dashboard')}>Return to Dashboard</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

interface EditorProps {
  variants: Slide[][];
  theme: Theme | null;
  logicalSlideCount: number;
  selectedSlideIndex: number;
  setSelectedSlideIndex(i: number | ((prev: number) => number)): void;
  selectedVariantBySlide: number[];
  setSelectedVariantBySlide(v: number[]): void;
  onExport(): void;
  isExporting: boolean;
}

const EditorView: React.FC<EditorProps> = ({
  variants,
  logicalSlideCount,
  selectedSlideIndex,
  setSelectedSlideIndex,
  selectedVariantBySlide,
  setSelectedVariantBySlide,
  onExport,
  isExporting,
}) => {
  const currentVariantIndex = selectedVariantBySlide[selectedSlideIndex] ?? 0;
  const slides = variants[currentVariantIndex] || variants[0];
  const current = slides[selectedSlideIndex];

  return (
    <div className="editor-layout">
      <div className="editor-columns">
        <aside className="nav-column card">
          <div className="column-header" style={{ marginBottom: '1rem', fontWeight: 700, opacity: 0.8 }}>Navigator</div>
          <div className="nav-list">
            {slides.map((s, idx) => (
              <div key={idx} className={`thumb ${idx === selectedSlideIndex ? "thumb-active" : ""}`} onClick={() => setSelectedSlideIndex(idx)}>
                <span className="thumb-index">{idx + 1 < 10 ? `0${idx + 1}` : idx + 1}</span>
                <div className="thumb-title" style={{ fontSize: '0.85rem', fontWeight: 500 }}>{s.content.title || "Untitled"}</div>
              </div>
            ))}
          </div>
        </aside>

        <div className="preview-column card">
          <div className="preview-frame">
            <div className="preview-slide" style={{ position: 'relative' }}>
              {current.content.generated_image_path && (
                <div style={{
                  position: 'absolute',
                  right: '3rem',
                  top: '50%',
                  transform: 'translateY(-50%)',
                  width: '35%',
                  aspectRatio: '4/3',
                  background: '#1a1f2e',
                  borderRadius: '12px',
                  overflow: 'hidden',
                  boxShadow: '0 20px 40px rgba(0,0,0,0.5)'
                }}>
                  <img src={`http://localhost:4001/${current.content.generated_image_path}`} alt="Slide ref" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                </div>
              )}
              <h2 className="preview-title" style={{ maxWidth: current.content.generated_image_path ? '60%' : '100%' }}>{current.content.title}</h2>
              {current.content.bullets && (
                <ul className="preview-bullets" style={{ maxWidth: current.content.generated_image_path ? '60%' : '100%' }}>
                  {current.content.bullets.map((b, i) => <li key={i}>{b}</li>)}
                </ul>
              )}
            </div>
          </div>
          <div className="preview-controls" style={{ display: 'flex', justifyContent: 'space-between', marginTop: '1.5rem', alignItems: 'center' }}>
            <button className="nav-item" onClick={() => setSelectedSlideIndex(p => Math.max(0, p - 1))}>Back</button>
            <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Slide {selectedSlideIndex + 1} / {logicalSlideCount}</span>
            <button className="nav-item" onClick={() => setSelectedSlideIndex(p => Math.min(logicalSlideCount - 1, p + 1))}>Next</button>
          </div>
        </div>

        <div className="edit-column-alt card">
          <div className="column-header" style={{ marginBottom: '1.5rem', fontWeight: 700, opacity: 0.8 }}>Visual Variants</div>
          <div className="variant-strip">
            {variants.map((vset, vi) => (
              <div
                key={vi}
                className={`variant-card ${selectedVariantBySlide[selectedSlideIndex] === vi ? "variant-card-active" : ""}`}
                onClick={() => {
                  const copy = [...selectedVariantBySlide];
                  copy[selectedSlideIndex] = vi;
                  setSelectedVariantBySlide(copy);
                }}
              >
                <div className="variant-badge" style={{ fontSize: '0.7rem', color: 'var(--accent-primary)', marginBottom: '0.25rem' }}>Layout {vi + 1}</div>
                <div className="variant-title" style={{ textTransform: 'capitalize', fontWeight: 600 }}>{vset[selectedSlideIndex].content_type.replace('_', ' ')}</div>
              </div>
            ))}
          </div>
          <div style={{ flex: 1 }}></div>
          <button className="btn-generate" onClick={onExport} disabled={isExporting}>
            {isExporting ? "EXPORTING..." : "EXPORT PPTX"}
          </button>
        </div>
      </div>
    </div>
  );
};

export default App;
