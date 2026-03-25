import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';

@Component({
  selector: 'app-blog-landing',
  standalone: true,
  imports: [CommonModule, MatButtonModule, MatIconModule],
  templateUrl: './blog-landing.component.html',
  styleUrl: './blog-landing.component.scss',
})
export class BlogLandingComponent {
  private readonly router = inject(Router);

  navigateToDashboard(): void {
    this.router.navigate(['/blogging/dashboard']);
  }

  navigateToFullPipeline(): void {
    this.router.navigate(['/blogging/dashboard'], { queryParams: { tab: 'full-pipeline' } });
  }

  navigateToResearch(): void {
    this.router.navigate(['/blogging/dashboard'], { queryParams: { tab: 'research' } });
  }

  readonly pipelinePhases = [
    {
      icon: 'travel_explore',
      title: 'Research',
      description: 'AI scours the web and arXiv for relevant sources, ranks them, and compiles a research document.',
    },
    {
      icon: 'architecture',
      title: 'Planning',
      description: 'Generates a structured content plan with title options, narrative flow, and per-section coverage.',
    },
    {
      icon: 'edit_note',
      title: 'Drafting',
      description: 'Produces a full draft from your research and plan, guided by your brand voice and style.',
    },
    {
      icon: 'rate_review',
      title: 'Copy Edit',
      description: 'An AI editor reviews structure, clarity, and tone with actionable feedback and revisions.',
    },
    {
      icon: 'fact_check',
      title: 'Fact Check',
      description: 'Extracts claims, assesses risk, and flags anything that needs verification before publishing.',
    },
    {
      icon: 'verified',
      title: 'Compliance',
      description: 'Enforces your brand spec and writing guidelines. Hard gate: a failure blocks publication.',
    },
    {
      icon: 'loop',
      title: 'Rewrite',
      description: 'If any gate fails, targeted fixes are applied and gates re-run automatically (up to 3 rounds).',
    },
    {
      icon: 'publish',
      title: 'Finalize',
      description: 'Generates platform-specific versions for Medium, dev.to, and Substack, plus a publishing pack.',
    },
  ];

  readonly features = [
    {
      icon: 'tune',
      title: 'Content Profiles',
      description: 'Choose from short listicle, standard article, technical deep dive, or series instalment.',
    },
    {
      icon: 'people',
      title: 'Audience Targeting',
      description: 'Define skill level, profession, and interests so the AI tailors language and depth.',
    },
    {
      icon: 'auto_stories',
      title: 'Story Bank',
      description: 'A ghost writer interviews you for first-person narratives, stored for reuse across posts.',
    },
    {
      icon: 'chat',
      title: 'Interactive Collaboration',
      description: 'Choose titles, answer questions, and share stories as the pipeline works alongside you.',
    },
    {
      icon: 'analytics',
      title: 'Medium Analytics',
      description: 'Scrape your Medium dashboard stats to inform content strategy with real performance data.',
    },
    {
      icon: 'inventory_2',
      title: 'Rich Artifacts',
      description: '16 versioned artifacts from research packets to compliance reports, all downloadable.',
    },
  ];
}
