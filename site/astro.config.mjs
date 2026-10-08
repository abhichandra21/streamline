// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import mermaid from 'astro-mermaid';

export default defineConfig({
	integrations: [
		// Must come before starlight so ```mermaid blocks render as diagrams.
		mermaid({ theme: 'dark', autoTheme: true }),
		starlight({
			title: 'Streamline',
			description: 'A personal streaming recommendation engine that knows your actual taste.',
			logo: { src: './src/assets/logo.png' },
			customCss: ['./src/styles/theme.css'],
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/abhichandra21/streamline' }],
			editLink: { baseUrl: 'https://github.com/abhichandra21/streamline/edit/master/site/' },
			lastUpdated: true,
			sidebar: [
				{ label: 'Start here', items: ['getting-started'] },
				{
					label: 'Using Streamline',
					items: [
						'guides/search',
						'guides/mood-match',
						'guides/find',
						'guides/on-deck',
						'guides/archive',
						'guides/watchlist-and-searches',
						'guides/settings',
					],
				},
				{
					label: 'Connections',
					items: ['guides/docker', 'guides/watch-history', 'guides/plex', 'guides/home-assistant', 'guides/running-as-a-service'],
				},
				{
					label: 'Reference',
					items: ['reference/cli', 'reference/configuration', 'reference/architecture', 'reference/roadmap'],
				},
			],
		}),
	],
});
