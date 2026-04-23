package main

import (
	"encoding/json"
	"log"
	"net/http"
	"time"
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

func handler(w http.ResponseWriter, r *http.Request) {
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
}

func main() {
	http.HandleFunc("/", handler)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
