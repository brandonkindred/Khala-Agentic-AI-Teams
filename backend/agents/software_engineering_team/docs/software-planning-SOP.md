
## Phase 1 - Identify environment constraints and other high level requirements

### Sub-phase 1 - Deployments
	P1.deploy.a - Where will this be deployed? (On-prem, Cloud, Platofrm as a Service such as Heroku, Supabase, Vercel, etc., etc. ), DigitalOcean, etc..
	P1.deploy.b - If it will be deployed in the Cloud, which provider is preferred (GCP, AWS, Azure, RackSpace, etc.)?
	P1.deploy.c - If it will be deployed in the cloud, does the user want to use serverless?
	P1.deploy.d - If it will be deployed to a Platform as a Service(PaaS), which one? (Heroku, Supabase, Vercel, etc.)

### Sub-phase 2 - Regulations
	P1.regulations.a - Is the user subject to regulations such as GDPR, California digital privacy, HIPPA, etc.?
	P1.regulations.b - Will the user need to prove enterprise certifications such as SOC2 or other enterprise software certification processes?
### Sub-phase 3 - Tool/Service preferences
	P1.tools.a - Stance on OpenSource tools/services? Does user prefer open source or proprietery tools? 
	P1.tools.b - If the user prefers proprietary tools, are there any existing tools that the user already has licenses for that we should plan on using?
	P1.tools.c - If the user prefers OpenSource, are there any tools specifically that they are already familiar with across the stack? Gathering this information helps with planning for implementing the stack that best suits the user.

### Sub-phase 4 - Coding preferences
	P1.coding.a - Where will work be stored? (GitHub, GitLab, BitBucket, Codeberg, etc.)
	P1.coding.b - Does the user prefer to use any specific languages for this project? (Java, Rust, Python, JavaScript, TypeScript, Erlang, Ruby, C, C++, etc., etc.)
	P1.coding.c - Does the user have any preferences for frameworks? For example, if the user wants to use Java do they want to use Spring, Spring Boot, Micronaut, etc.? If the user wants to create an application with a front-end user interface, do they want to use Angular, React, Vue, Svelt, etc.?
	P1.coding.d - Does the user have a preference for package management given the language that they chose?
	P1.coding.e - Which service should be used for a CI/CD pipeline? (GitHub, GitLab, AWS CodePipeline, CodeShip, etc. etc.)

### Sub-phase 5 - Data
	P1.data.a - What kind of data needs to be stored? (files, structured data, time series data, events for event sourcing, highly interconnected data (aka graph data), etc.
	P1.data.a.1 - Is there a preffered tool/services/provider that should be used for storing data. Some example services are PostGres, MySQL, Couch DB, OpenSearch, ElasticSearch, Neptune, S3, Google Storage,  
	P1.data.b - Are events or data streaming used? If so, how?
	P1.data.b.1 - If events or data streaming is used, are there any preferred tools/services?
	P1.data.b.2 - If events, is/does the system implement event sourcing? If so are, there preferred tools/services

### Sub-phase 6 - Security
	P1.security.a - Does user have a preference for which service to use for authenticaion and authorization? For example Auth0, Amazon Cognito, etc. 
	P1.security.b - Does the user have preferences for security focused services/tools such as Sentry, Amazon Web Application Firewall, Cloudflare, Snyk, Checkmarx, SonarQube, Burp Suite, Veracode, Aikido Security, Aqua Security, Palo Alto Prism Cloud, etc.
	P1.security.c.1 - Does the user have preferences for encryption key management such as Hashicorp Vault, AWS Secrets Manager, Google Secrets, etc.?
	P1.security.c.2 - Will user manage keys or do they want to use a managed key in the cloud?

### Sub-phase 7 - Observability / Logging
	P1.obseravability.a - Does the user have preferences in terms of what tools to use for observability such as logging, metrics gathering, reporting, etc.? For example Prometheus, Grafana, AWS X-Ray, Amazon CloudWatch, Google Cloud Logging, Google Cloud trace, etc.

### Sub-phase 8 - Service Level Agreements
	P1.network.latency.1 - Are there any requirements or SLA for response time? (ie, 15 second API response time, user receives real-time updates, )
	P1.network.robustness - Are there any requirements for uptime or data loss? ( Recovery Point Objective(RPO) of 4 hours, or uptime of 99.9%, or RTO of 5 minutes, etc.)

### Sub-phase 9 - Budget
	P1.budget.a - Is there a budget that this solution needs to stay within?
	P1.budget.b - Is the budget flexible(we can exceed it) or rigid(max spend without exception)? 

### Sub-phase 10 - How should decisions be prioritized?
	P1.priorities - What are the priorities?(Resiliency/Performance/Frugality/Simplicity/etc.)
	P1.priorities.b - If there are multiple priorities, what order should they be considered in terms of most important to least important?

## Phase 2 - High Level Planning (Architecture) - Maps the high level flows of user interactions/data/events/etc and generates software architecture diagrams

	P2.architecture.a - Which architecture works best for the spec and the requirements from phase 1 (2-tier, 3-tier, 4-tier/N-tier)? 
	P2.architecture.b - What types of data exist and which services are needed for data storage?
	P2.architecture.c - What types of tasks are being performed ( Heavy Compute, Memory intensive/GPU intensive/etc.)
	P2.architecture.d.1 - Identify service/tool gaps where the list of preferred services/tools don't fully cover the requirements based on the provided specification.
	P2.architecture.d.2 - For each service/tool gap that is identified, recommend 3-5 tools with a brief 1-2 sentence description of what the tools does and why it's used. You should identify one or more tools that you recommend. Only recommend more than one tool if the recommended tools/services compliment each other in the stack and work together well. Don't recommend multiple tools or services if there is significant overlap between what the tools do or if they are notoriously difficult to get to work together. 
	P2.architecture.e - Generate a set of architecture diagrams that demonstrate how the system will function from various levels of granularity and detail. 