# Data
I have the following three files saved in directory ./data/

	1. invoices_2025_clean.csv: a table with following columns:
		- invoice_number: unique string indicating invoice number
		- invoice_date: date of the invoice
		- vendor_id: unique string indicating vendor id
		- vendor_name: name of the vendor
		- description: description of the procurement
		- amount: float number in two decimal place indicating the amount of purchase
		- currency: the currency of the purchase, can be one of {'EUR', 'GBP', 'USD'}; use the authorized ECB euro foreign exchange reference rates for invoice-date conversion as described below
		- cost_center: cost center for the purchase
		- po_number: number of the approved Purchase Order (PO), some rows have missing values in this column
		- approved_by: name of the PO approving the purchase, some rows have missing values in this column
	2. vendor_master.csv: a table with following columns:
		- vendor_id: unique string indicating vendor id
		- vendor_name: name of the vendor
		- country: country of the vendor
		- category: category of the vendor
		- status: status of the vendor, can be one of {'ACTIVE', 'INACTIVE', 'ON_HOLD'}
	3. procurement_policy.md: a markdown file containing the global procurement and spend policy

The only additional evidence source is the [ECB euro foreign exchange reference rates](https://data.ecb.europa.eu/data/datasets/EXR) published on the ECB Data Portal: the daily [US dollar series EXR.D.USD.EUR.SP00.A](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.USD.EUR.SP00.A) and [pound sterling series EXR.D.GBP.EUR.SP00.A](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.GBP.EUR.SP00.A), fetched from the ECB Data Portal API (`https://data-api.ecb.europa.eu/service/data/EXR/...`). Both series quote units of the foreign currency per 1 EUR and are published on TARGET business days only. Use these observations for deterministic conversion among EUR, GBP, and USD; never use rates recalled or invented by GPT or another external source.

Default consolidated totals and rankings to EUR, while retaining the original-currency breakdown. Distinguish reporting in EUR from filtering to invoices originally denominated in EUR. Application code fetches, validates, and caches the authorized rates; GPT-generated functions have no network access. Every converted result must cite the rate series, source URL, observation date, requested invoice date, rate values, conversion method, and source snapshot.

Use the invoice-date observation when available. The proposed default for a weekend, holiday, or other missing observation is the latest prior date with valid rates for all required currency legs, no more than 7 calendar days earlier. Use the same observation date for both legs of a cross-rate, show the actual date and fallback clearly, and never use a future observation. The ECB publishes no reference rates on weekends or TARGET holidays (1 January, Good Friday, Easter Monday, 1 May, 25 and 26 December); this fallback is a prototype convention, not a rule stated by the ECB or the policy. An explicit exact-date request disables fallback. If no acceptable rate exists, identify the missing evidence and recommend review; never omit affected invoices from an apparently complete converted total. The implementation plan defines the conversion formula, Decimal precision, rounding, cache, and evidence requirements.

Preserve `procurement_policy.md` as supplied. Its §5.1 specifies the ECB reference rate on the invoice date, and this application uses ECB reference rates as directed, so converted answers state that basis. Where the invoice date had no ECB publication and an earlier rate was used, say so clearly: §5.1 does not address such dates, so those conversions are flagged for review rather than presented as following the policy to the letter. Policy answers must still accurately quote what the policy says.

*Change note (14 Nov 2026): the authorized rate source was changed from Federal Reserve H.10 to ECB euro reference rates at the user's direction.*

# Objective
Build a web app with FastAPI as backend, React as frontend, where a finance analyst can ask questions about the invoice data saved in invoices_2025_clean.csv and vendor_master.csv, and the global procurement and spend policy. Below are three example questions:

	- What are our top 5 vendors by total spend in 2025?
	- What is our total spend with vendors based in Spain?
	- Who is allowed to approve an invoice of EUR 60,000?

# Requirements
## Backend
Two endpoints, minimum

	- POST /ask: takes a question, returns an answer and the evidence behind it (which part of the policy, which rows from the data)
	- GET /summary: returns a quick overview of the invoice data (consolidated EUR spend with ECB reference-rate evidence, original-currency totals, and vendor-status breakdowns). If required rates are unavailable, keep the original-currency summary usable and clearly mark consolidated metrics unavailable; never show a partial conversion as a complete total

Follow the following plan-and-solve agentic workflow when designing the backend

	1. after receiving the user query, use GPT model (with OPENAI_API_KEY saved in .env) to decide which data source to use
		- the global procurement and spend policy specified in procurement_policy.md
		- the invoice and vendor records saved in those two csv file
		- ECB euro reference rates when the data question needs currency conversion; this supplements the CSV evidence rather than replacing it
		- if the user query is irrelevant, such as "what is today's weather?", respond the input query cannot be supported by any evidence in the data. The LLM will only answer questions supported by those three files and the authorized ECB euro reference rates
	2. if procurement_policy.md is chosen in step 1, use the content in procurement_policy.md as context of the GPT model to answer the query and list the supporting evidence. If the query cannot be supported by any clause in procurement_policy.md, respond no evidence can be found to answer the question. Never use GPT's internal knowledge to answer without getting support from the data
	3. if the invoice and vendor records saved in those two csv file are chosen in step 1, given the table schema as context, use GPT to convert the user query into pandas query function, and return the query results as well as records supporting the results. When conversion is needed, expose validated application-computed reporting amounts and ECB rate references in the schema. Use GPT to provide explanation on the verified results, grounded in the selected local records, policy clauses where relevant, and ECB reference-rate observations. If the user query cannot be successfully converted to pandas query function or the function cannot successfully run (e.g. due to the user's reference to columns not existing in those tables), return the explanation to the user so that the user can update the query accordingly. Missing or invalid rates must produce an explicit limitation or review response rather than guessed rates or an incomplete consolidated answer

The decision trajectory of the GPT for each input query can be saved so that they can be used for troubleshooting when getting unexpected results.

## Frontend
	- A chat panel where the analyst types questions and sees answers with their supporting evidence
	- A summary panel that loads on startup from GET /summary — there should be something on screen before the first question is asked
	- The UI design should have high contrast so that the information is easy to read on a computer screen

We are looking at how you handle state, errors, loading, and — most importantly — how the UI makes it clear what the answer is based on.