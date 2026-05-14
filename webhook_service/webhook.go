package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	cloudtasks "cloud.google.com/go/cloudtasks/apiv2"
	taskspb "cloud.google.com/go/cloudtasks/apiv2/cloudtaskspb"
	"cloud.google.com/go/firestore"
	"cloud.google.com/go/firestore/apiv1/firestorepb"
)

const (
	UpVote   = "👍"
	DownVote = "👎"

	telegramSecretHeader = "X-Telegram-Bot-Api-Secret-Token"
	maxRequestBodyBytes  = 1 << 20 // 1 MiB — Telegram updates are far smaller than this
)

type Config struct {
	ProjectID             string
	LocationID            string
	QueueID               string
	PapersCollectionName  string
	ProfileCollectionName string
	GenerationURL         string
	WebhookSecret         string
	VoteThreshold         int
}

type Update struct {
	UpdateID               int64                        `json:"update_id"`
	MessageReactionCount   *MessageReactionCountUpdated `json:"message_reaction_count,omitempty"`
	MessageReactionUpdated *MessageReactionUpdated      `json:"message_reaction,omitempty"` // Don't actually need to handle this atm.
}

type MessageReactionCountUpdated struct {
	Chat      Chat            `json:"chat"`
	MessageID int64           `json:"message_id"`
	Date      int64           `json:"date"`
	Reactions []ReactionCount `json:"reactions"`
}

type MessageReactionUpdated struct {
}

type ReactionCount struct {
	Type  *ReactionTypeEmoji `json:"type,omitempty"`
	Count int64              `json:"total_count,omitempty"`
}

type ReactionTypeEmoji struct {
	Type  string `json:"type,omitempty"`
	Emoji string `json:"emoji,omitempty"`
}

type Chat struct {
	ID       int64  `json:"id"`
	Type     string `json:"type"`
	Title    string `json:"title,omitempty"`
	Username string `json:"username,omitempty"`
}

type Server struct {
	cfg       *Config
	firestore *firestore.Client
	tasks     *cloudtasks.Client
}

func mustGetEnv(key string) string {
	v, ok := os.LookupEnv(key)
	if !ok {
		panic(fmt.Sprintf("required environment variable %s is not set", key))
	}
	if v == "" {
		panic(fmt.Sprintf("required environment variable %s is empty", key))
	}
	return v
}

func loadConfig() *Config {
	vote_threshold, err := strconv.Atoi(mustGetEnv("VOTE_THRESHOLD"))
	if err != nil {
		panic("Required environment variable VOTE_THRESHOLD is not of the correct type.")
	}
	return &Config{
		ProjectID:             mustGetEnv("GCP_PROJECT_ID"),
		LocationID:            mustGetEnv("LOCATION"),
		QueueID:               mustGetEnv("QUEUE_ID"),
		PapersCollectionName:  mustGetEnv("PAPERS_COLLECTION_NAME"),
		ProfileCollectionName: mustGetEnv("PROFILE_COLLECTION_NAME"),
		GenerationURL:         mustGetEnv("GENERATION_URL"),
		WebhookSecret:         mustGetEnv("WEBHOOK_SECRET"),
		VoteThreshold:         vote_threshold,
	}
}

func calculateScore(reactions MessageReactionCountUpdated) (int64, int64) {
	var downVotes int64
	var upVotes int64
	for _, reaction := range reactions.Reactions {
		if reaction.Type == nil {
			continue
		}
		if reaction.Type.Emoji == UpVote {
			upVotes = reaction.Count
		}
		if reaction.Type.Emoji == DownVote {
			downVotes = reaction.Count
		}
	}
	return upVotes - downVotes, reactions.MessageID
}

func updateScore(ctx context.Context, client *firestore.Client, config Config, messageID int64, score int64, timestamp time.Time) error {
	_, err := client.Collection(config.PapersCollectionName).Doc(fmt.Sprintf("%d", messageID)).Update(ctx, []firestore.Update{
		{
			Path:  "score",
			Value: score,
		},
		{
			Path:  "last_vote_at",
			Value: timestamp,
		},
	})
	if err != nil {
		return fmt.Errorf("updating score for message %d: %w", messageID, err)
	}
	return nil
}

func getUpdatedCount(ctx context.Context, client *firestore.Client, config Config) (int64, error) {
	query := client.Collection(config.ProfileCollectionName).OrderBy("generated_at", firestore.Desc).Limit(1)
	fetchedProfiles, err := query.Documents(ctx).GetAll()
	if err != nil {
		return 0, err
	}
	if len(fetchedProfiles) == 0 {
		return 10000000, nil // No profile we want to regenerate anyway.
	}
	latestProfile := fetchedProfiles[0].Data()
	last_profile_timestamp, ok := latestProfile["generated_at"].(time.Time)
	if !ok {
		return 0, errors.New("Parse error when retrieving last profile time stamp.")
	}
	query = client.Collection(config.PapersCollectionName).Where("last_vote_at", ">", last_profile_timestamp)
	aggregationQuery := query.NewAggregationQuery().WithCount("all")
	results, err := aggregationQuery.Get(ctx)
	if err != nil {
		return 0, err
	}

	count, ok := results["all"]
	if !ok {
		return 0, errors.New("firestore: couldn't get alias for COUNT from results")
	}

	updatedCount := count.(*firestorepb.Value).GetIntegerValue()
	return updatedCount, nil
}

func queueProfileGenerationTask(ctx context.Context, client *cloudtasks.Client, queueID string, url string) error {

	req := &taskspb.CreateTaskRequest{
		Parent: queueID,
		Task: &taskspb.Task{
			MessageType: &taskspb.Task_HttpRequest{
				HttpRequest: &taskspb.HttpRequest{
					HttpMethod: taskspb.HttpMethod_POST,
					Url:        url,
				},
			},
		},
	}

	_, err := client.CreateTask(ctx, req)
	if err != nil {
		return fmt.Errorf("cloudtasks.CreateTask: %w", err)
	}

	return nil
}

func validUpdate(u Update) bool {
	if u.MessageReactionCount != nil {
		log.Println("Message reaction count updated.")
		return true
	}

	if u.MessageReactionUpdated != nil {
		log.Println("Message reaction updated.")
		return true
	}
	log.Println("Unknown type.")
	return false
}

func validSecret(r *http.Request, expected string) bool {
	got := r.Header.Get(telegramSecretHeader)
	return subtle.ConstantTimeCompare([]byte(got), []byte(expected)) == 1
}

func (s *Server) handler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !validSecret(r, s.cfg.WebhookSecret) {
		log.Println("Rejected request: invalid or missing Telegram secret token header.")
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	ctx := r.Context()
	var u Update
	err := json.NewDecoder(r.Body).Decode(&u)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if !validUpdate(u) {
		http.Error(w, "Invalid Webhook Type", http.StatusBadRequest)
		return
	}
	score, id := calculateScore(*u.MessageReactionCount)
	timestamp := time.Unix(u.MessageReactionCount.Date, 0)
	if err := updateScore(ctx, s.firestore, *s.cfg, id, score, timestamp); err != nil {
		log.Printf("failed to update score: %v", err)
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	updatedCount, err := getUpdatedCount(ctx, s.firestore, *s.cfg)
	if err != nil {
		log.Printf("Failed to get updated papers count: %v", err)
		http.Error(w, "Internal server error", http.StatusBadRequest)
		return
	}
	if updatedCount > int64(s.cfg.VoteThreshold) {
		err = queueProfileGenerationTask(ctx, s.tasks, s.cfg.QueueID, s.cfg.GenerationURL)
		if err != nil {
			log.Printf("Failed to queue regeneration %v", err)
		}
	}
	w.WriteHeader(http.StatusOK)
}

func main() {
	ctx := context.Background()
	cfg := loadConfig()
	fsClient, err := firestore.NewClient(ctx, cfg.ProjectID)
	if err != nil {
		log.Fatalf("failed to create firestore client: %v", err)
	}
	defer fsClient.Close()
	tasksClient, err := cloudtasks.NewClient(ctx)
	if err != nil {
		log.Fatalf("failed to create cloud tasks client: %v", err)
	}
	defer tasksClient.Close()

	server := Server{
		cfg:       cfg,
		firestore: fsClient,
		tasks:     tasksClient,
	}

	http.HandleFunc("/", server.handler)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
