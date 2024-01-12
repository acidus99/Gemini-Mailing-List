(defonce form-entries (atom {}))

(def form-template (clojure.string/trim "
# Welcome to my Gemini Form!

To fill in any field below, simply click it. Everything's a link in Gemini, so you can't really mess up!

=> form.clj?%1$s&name Name: %2$s

=> form.clj?%1$s&password Password: %3$s

=> form.clj?%1$s&smog SMOG is great: %4$s

=> form.clj?%1$s&plant Best Astrobotany Plant: %5$s

=> form.clj?%1$s&submit Submit Answers
"))

(defn show-form [session]
  (let [{:strs [name password smog plant]} (@form-entries session)]
    {:status 20
     :meta   "text/gemini; charset=utf-8"
     :body   (format form-template
                     session
                     (or name "")
                     (apply str (repeat (count password) \*))
                     (or smog "")
                     (or plant ""))}))

(def prompts {"name"     {:sensitive? false :prompt "Enter your name"}
              "password" {:sensitive? true  :prompt "Enter your password"}
              "smog"     {:sensitive? false :prompt "Choose one of [Yes|No]"}
              "plant"    {:sensitive? false :prompt "Choose one of [Ficus|Baobob|Pachypodium|Moss]"}})

(defn show-prompt [field]
    (let [{:keys [sensitive? prompt]} (prompts field)]
      {:status (if sensitive? 11 10)
       :meta   prompt}))

(defn new-session []
  (subs (str (java.util.UUID/randomUUID)) 0 8))

(defn main [{:keys [params]}]
  (let [[session field value] params]
    (if-not session
      (show-form (new-session))
      (if (= field "submit")
        (show-form session)
        (if value
          (do
            (swap! form-entries assoc-in [session field] value)
            (show-form session))
          (show-prompt field))))))
