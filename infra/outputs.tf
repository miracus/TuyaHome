output "dashboard_urls" {
  description = "Особисте посилання кожного користувача: terraform output -json dashboard_urls"
  value       = { for u, p in random_password.web_key : u => "${aws_lambda_function_url.web.function_url}?k=${p.result}" }
  sensitive   = true
}

output "table" {
  value = aws_dynamodb_table.main.name
}

output "poll_schedule" {
  value = aws_cloudwatch_event_rule.poll.schedule_expression
}
