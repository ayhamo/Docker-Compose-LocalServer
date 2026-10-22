# Local override (mounted from /opt/docker/sure/overrides): Yahoo Finance answers
# 429 to the Firefox user agent in Provider::YahooFinance::USER_AGENTS, and a
# provider instance keeps the agent it drew, so one in five instances fails every
# request. Keep only the agents Yahoo accepts. Safe to delete once upstream fixes it.
Rails.application.config.after_initialize do
  if defined?(Provider::YahooFinance::USER_AGENTS)
    accepted = Provider::YahooFinance::USER_AGENTS.reject { |ua| ua.include?("Firefox") }
    if accepted.any? && accepted.size < Provider::YahooFinance::USER_AGENTS.size
      Provider::YahooFinance.send(:remove_const, :USER_AGENTS)
      Provider::YahooFinance.const_set(:USER_AGENTS, accepted.freeze)
    end
  end
end
