variable project_name {
    type = string
    description = "GCP Project Name"
}

variable "project_number" {
  type = string
  description = "GCP Project Number"
}

variable "region" {
  type = string
  description = "Region for the services."
}

variable "arxiv_categories" {
  type = string
  description = "Categories to fetch from arxiv."
}

variable "arxiv_max_results" {
  type = number
  description = "Number of results to fetch from arxiv."
}

variable "top_n_papers" {
  type = number
  default = 5
  description = "Number of papers to summarise."
}

variable "claude_model_ranking" {
  type = string
  description = "Claude model to use for ranking."
}

variable "claude_model_summary" {
  type = string
  description = "Claude model to use for summarisation."
}

variable "papers_collection" {
  type        = string
  default     = "sent_papers"
  description = "Firestore collection for sent papers."
}

variable "profiles_collection" {
  type        = string
  default     = "profiles"
  description = "Firestore collection for profiles."
}

variable "unvoted_ttl_days" {
  type        = number
  default     = 14
  description = "TTL in days for papers with no votes."
}

variable "claude_model_profile" {
  type        = string
  description = "Claude model to use for profile generation."
}

variable "vote_threshold" {
  type        = number
  default     = 10
  description = "Total votes required before the webhook triggers profile regeneration."
}