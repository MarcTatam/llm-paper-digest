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

variable "image" {
  type = string
  description = "Name of the image to use."
}