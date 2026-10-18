-- 0016 beat progress for the Hermes-style onboarding state machine (config/onboarding.yaml)
ALTER TABLE onboarding_plans ADD COLUMN beat_record TEXT;
