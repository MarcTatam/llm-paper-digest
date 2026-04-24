package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"cloud.google.com/go/firestore"
)

const (
	UpVote   = "👍"
	DownVote = "👎"
)

type Update struct {
	UpdateID                    int64                        `json:"update_id"`
	Message                     *Message                     `json:"message,omitempty"`
	EditedMessage               *Message                     `json:"edited_message,omitempty"`
	CallbackQuery               *CallbackQuery               `json:"callback_query,omitempty"`
	MessageReactionCountUpdated *MessageReactionCountUpdated `json:"message_reaction,omitempty"`
}

type MessageReactionCountUpdated struct {
	Chat      Chat            `json:"chat"`
	MessageID int64           `json:"message_id"`
	Date      int64           `json:"date"`
	Reactions []ReactionCount `json:"reactions"`
}

type ReactionCount struct {
	Type  *ReactionTypeEmoji `json:"type,omitempty"`
	Count int64              `json:"total_count,omitempty"`
}

type ReactionTypeEmoji struct {
	Type  string `json:"type,omitempty"`
	Emoji string `json:"emoji,omitempty"`
}

type Message struct {
	MessageID      int64    `json:"message_id"`
	From           *User    `json:"from,omitempty"`
	Chat           Chat     `json:"chat"`
	Date           int64    `json:"date"`
	Text           string   `json:"text,omitempty"`
	Entities       []Entity `json:"entities,omitempty"`
	ReplyToMessage *Message `json:"reply_to_message,omitempty"`
}

type User struct {
	ID           int64  `json:"id"`
	IsBot        bool   `json:"is_bot"`
	FirstName    string `json:"first_name"`
	LastName     string `json:"last_name,omitempty"`
	Username     string `json:"username,omitempty"`
	LanguageCode string `json:"language_code,omitempty"`
}

type Chat struct {
	ID       int64  `json:"id"`
	Type     string `json:"type"`
	Title    string `json:"title,omitempty"`
	Username string `json:"username,omitempty"`
}

type Entity struct {
	Type   string `json:"type"`
	Offset int    `json:"offset"`
	Length int    `json:"length"`
	URL    string `json:"url,omitempty"`
}

type CallbackQuery struct {
	ID      string   `json:"id"`
	From    User     `json:"from"`
	Message *Message `json:"message,omitempty"`
	Data    string   `json:"data,omitempty"`
}

func (m *Message) Time() time.Time {
	return time.Unix(m.Date, 0)
}

func createClient(ctx context.Context) *firestore.Client {
	// Sets your Google Cloud Platform project ID.
	projectID := "YOUR_PROJECT_ID"

	client, err := firestore.NewClient(ctx, projectID)
	if err != nil {
		log.Fatalf("Failed to create client: %v", err)
	}
	// Close client when done with
	// defer client.Close()
	return client
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

func updateScore(ctx context.Context, client *firestore.Client, messageID int64, score int64, timestamp time.Time) error {
	_, err := client.Collection("").Doc(fmt.Sprintf("%d", messageID)).Set(ctx, map[string]interface{}{ // TODO: Set Collection
		"score":     score,
		"timestamp": timestamp,
	})
	if err != nil {
		return fmt.Errorf("updating score for message %d: %w", messageID, err)
	}
	return nil
}

func handler(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	client := createClient(ctx)
	var u Update
	err := json.NewDecoder(r.Body).Decode(&u)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if u.MessageReactionCountUpdated == nil {
		http.Error(w, "Invalid Webhook Type", http.StatusBadRequest)
		return
	}
	score, id := calculateScore(*u.MessageReactionCountUpdated)
	timestamp := time.Unix(u.MessageReactionCountUpdated.Date, 0)
	if err := updateScore(ctx, client, id, score, timestamp); err != nil {
		log.Printf("failed to update score: %v", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func main() {
	ctx := context.Background()
	client, err := firestore.NewClient(ctx, "") // TODO: Set Project ID
	if err != nil {
		log.Fatalf("failed to create firestore client: %v", err)
	}
	defer client.Close()

	http.HandleFunc("/", handler)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
