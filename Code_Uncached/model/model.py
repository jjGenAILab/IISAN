import torch
from torch import nn
from torch.nn.init import xavier_normal_
from .encoders import Vit_Encoder,User_Encoder,Bert_Encoder,MM_Encoder,Vit_EncoderFFT,Bert_EncoderFFT
from .modules import AdapterBlock
from transformers.modeling_outputs import BaseModelOutput
from torch.autograd import Variable
import torch.nn.functional as F

def get_sim(images, captions):
    similarities = images.mm(captions.t())
    return similarities

class ModelMM(torch.nn.Module):
    def __init__(self, args, item_num, use_modal, image_net,bert_model, pop_prob_list):
        super(ModelMM, self).__init__()
        self.args = args
        self.use_modal = use_modal
        self.max_seq_len = args.max_seq_len
        self.l2_weight = args.l2_weight/2
        self.pop_prob_list = torch.FloatTensor(pop_prob_list)

        self.user_encoder = User_Encoder(
            item_num=item_num,
            max_seq_len=args.max_seq_len,
            item_dim=args.embedding_dim,
            num_attention_heads=args.num_attention_heads,
            dropout=args.drop_rate,
            n_layers=args.transformer_block)

        if self.use_modal:
            self.mm_encoder = MM_Encoder(args,image_net,bert_model)
        else:
            self.id_embedding = nn.Embedding(item_num + 1, args.embedding_dim, padding_idx=0)
            xavier_normal_(self.id_embedding.weight.data)
        if "intra_inter" in self.args.modality:
            self.com_dense = nn.Linear(args.embedding_dim * 3, args.embedding_dim)
        elif "inter" in self.args.modality:
            self.com_dense = nn.Linear(args.embedding_dim, args.embedding_dim)
        else:
            self.com_dense = nn.Linear(args.embedding_dim*2, args.embedding_dim)
        self.criterion = nn.CrossEntropyLoss()
        

    def reg_loss(self, parameters):
        reg_loss = 0
        for name, parm in parameters:
            if parm.requires_grad and 'LayerNorm' not in name and 'weight' in name:
                reg_loss = reg_loss + torch.sum(parm**2)
        return reg_loss

    def calculate_reg_loss(self, item_embedding):
        l2_reg = 0
        l2_reg = l2_reg + self.reg_loss(self.user_encoder.named_parameters())
        if self.use_modal:
            l2_reg = l2_reg + self.reg_loss(self.cv_encoder.named_parameters())
        else:
            l2_reg = l2_reg + torch.sum(item_embedding ** 2)
        return l2_reg

    def forward(self, sample_items_id,sample_items_images, sample_items_text, log_mask, local_rank):

        self.pop_prob_list = self.pop_prob_list.to(local_rank)
        debias_logits = torch.log(self.pop_prob_list[sample_items_id.view(-1)])
        # mutimodality fusion
        score_embs_cv, score_embs_text = self.mm_encoder(sample_items_images, sample_items_text)
        if "intra_inter" in self.args.modality:
            score_embs_text, score_embs_mm = score_embs_text
            score_embs = self.com_dense(torch.cat([score_embs_cv, score_embs_text, score_embs_mm], dim=1))
        elif "inter" in self.args.modality:
            score_embs_text,score_embs_mm = score_embs_text
            score_embs = self.com_dense(score_embs_mm)
        else:
            score_embs = self.com_dense(torch.cat([score_embs_cv,score_embs_text],dim=1))

        input_embs = score_embs.view(-1, self.max_seq_len + 1, self.args.embedding_dim)

        prec_vec = self.user_encoder(input_embs[:, :-1, :], log_mask, local_rank)
        prec_vec = prec_vec.view(-1, self.args.embedding_dim)  # (bs*max_seq_len, ed)

        ######################################  IN-BATCH CROSS-ENTROPY LOSS  ######################################
        bs = log_mask.size(0)
        ce_label = torch.tensor(
            [i * self.max_seq_len + i + j for i in range(bs) for j in range(1, self.max_seq_len + 1)],
            dtype=torch.long).to(local_rank)
        logits = torch.matmul(prec_vec, score_embs.t())  # (batch_size*max_seq_len, batch_size*(max_seq_len+1))
        logits = logits - debias_logits
        logits[:, torch.cat((log_mask, torch.ones(log_mask.size(0))
                             .unsqueeze(-1).to(local_rank)), dim=1).view(-1) == 0] = -1e4
        logits = logits.view(bs, self.max_seq_len, -1)
        id_list = sample_items_id.view(bs, -1)  # sample_items_id (bs, max_seq_len)
        for i in range(bs):
            reject_list = id_list[i]  # reject_list (max_seq_len)
            u_ids = sample_items_id.repeat(self.max_seq_len).expand((len(reject_list), -1))
            reject_mat = reject_list.expand((u_ids.size(1), len(reject_list))).t()
            # (max_seq_len, batch_size*(max_seq_len+1))
            mask_mat = (u_ids == reject_mat).any(axis=0).reshape(logits[i].shape)
            for j in range(self.max_seq_len):
                mask_mat[j][i * (self.max_seq_len + 1) + j + 1] = False
            logits[i][mask_mat] = -1e4

        indices = torch.where(log_mask.view(-1) != 0)
        logits = logits.view(bs * self.max_seq_len, -1)
        loss = self.criterion(logits[indices], ce_label[indices])
        return loss


class BertAdaptedSelfOutput(nn.Module):
    def __init__(self,
                 self_output,args):
        super(BertAdaptedSelfOutput, self).__init__()
        if 'tiny' in args.bert_model_load:
            word_embedding_dim = 128
        elif 'mini' in args.bert_model_load:
            word_embedding_dim = 256
        elif 'medium' in args.bert_model_load:
            word_embedding_dim = 512
        elif 'base' in args.bert_model_load:
            word_embedding_dim = 768
        elif 'large' in args.bert_model_load:
            word_embedding_dim = 1024
        else:
            assert 1==0,'The pretrained model name should be defined correctly. such as bert-base-uncased so on'
        self.self_output = self_output
        self.adapter = AdapterBlock(args,word_embedding_dim,args.bert_adapter_down_size,args.adapter_dropout_rate)

    def forward(self, hidden_states: torch.Tensor, input_tensor: torch.Tensor):
        hidden_states = self.self_output.dense(hidden_states)
        hidden_states = self.self_output.dropout(hidden_states)
        hidden_states = self.adapter(hidden_states)
        hidden_states = self.self_output.LayerNorm(hidden_states + input_tensor)
        return hidden_states
    

class VITAdaptedSelfOutput(nn.Module):
    def __init__(self,
                 self_output,args):
        super(VITAdaptedSelfOutput, self).__init__()
        embedding_dim = 768

        self.self_output = self_output
        self.adapter = AdapterBlock(args,embedding_dim,args.cv_adapter_down_size,args.adapter_dropout_rate)

    def forward(self, hidden_states: torch.Tensor, input_tensor: torch.Tensor):
        hidden_states = self.self_output.dense(hidden_states)
        hidden_states = self.self_output.dropout(hidden_states)
        hidden_states = self.adapter(hidden_states)
        return hidden_states

class VITAdaptedOutput(nn.Module):
    def __init__(self,
                 self_output,args):
        super(VITAdaptedOutput, self).__init__()
        embedding_dim = 768

        self.self_output = self_output
        self.adapter = AdapterBlock(args,embedding_dim,args.cv_adapter_down_size,args.adapter_dropout_rate)

    def forward(self, hidden_states: torch.Tensor, input_tensor: torch.Tensor):
        hidden_states = self.self_output.dense(hidden_states)
        hidden_states = self.self_output.dropout(hidden_states)
        hidden_states = self.adapter(hidden_states)
        hidden_states = hidden_states + input_tensor
        return hidden_states
    
class IISANAdaptedMModel(nn.Module):
    def __init__(self,mm_model,args):
        super(IISANAdaptedMModel,self).__init__()
        self.cv_encoder = mm_model.cv_encoder
        self.bert_encoder = mm_model.bert_encoder
        embedding_dim = 768
        if args.remove_first == "TRUE":
            self.side_bert_adapter_num_list = [int(i) + 1 for i in list(args.side_adapter_vit_list.split(","))]
            self.side_cv_adapter_num_list = [int(i) + 1 for i in list(args.side_adapter_bert_list.split(","))]
        else:
            self.side_bert_adapter_num_list = [0]+[int(i)+1 for i in list(args.side_adapter_vit_list.split(","))]
            self.side_cv_adapter_num_list = [0]+[int(i)+1 for i in list(args.side_adapter_vit_list.split(","))]
        if "intra" in args.modality:
            self.cv_adapter_list = nn.ModuleList([AdapterBlock(args,embedding_dim,args.cv_adapter_down_size,args.adapter_dropout_rate) for i in self.side_cv_adapter_num_list])
            self.bert_adapter_list = nn.ModuleList([AdapterBlock(args,args.word_embedding_dim,args.bert_adapter_down_size,args.adapter_dropout_rate) for i in self.side_bert_adapter_num_list])
        #assert len(self.bert_adapter_list) == len(self.cv_adapter_list),"The adapter list length do not match"
        if "inter" in args.modality:
            self.mm_adapter_list = nn.ModuleList([AdapterBlock(args,args.word_embedding_dim,args.bert_adapter_down_size,args.adapter_dropout_rate) for i in self.side_cv_adapter_num_list])

        if "intra" in args.modality:
            self.fc_bert = nn.Linear(embedding_dim, embedding_dim)
            self.fc_cv = nn.Linear(embedding_dim, embedding_dim)
        if "inter" in args.modality:
            self.fc_mm = nn.Linear(args.word_embedding_dim, args.word_embedding_dim)
            self.fc_mm_down = nn.Linear(args.word_embedding_dim, args.embedding_dim)
        if args.fusion_method == "gated":
            if "intra" in args.modality:
                self.side_gate_params_text = nn.ParameterList(
                        [nn.Parameter(torch.ones(1) * 0)
                        for i in range(len(self.cv_adapter_list))]
                    )
                self.side_gate_params_cv = nn.ParameterList(
                        [nn.Parameter(torch.ones(1) * 0)
                        for i in range(len(self.cv_adapter_list))]
                    )
            if "inter" in args.modality:
                self.side_gate_params_mm = nn.ParameterList(
                        [nn.Parameter(torch.ones(1) * 0)
                        for i in range(len(self.side_cv_adapter_num_list))]
                    )
           
        self.args = args
        
    def forward(self,sample_items_images, sample_items_text):
        score_embs_cv, hidden_states_cv = self.cv_encoder(sample_items_images) # score_embs_cv has a dimension of [bs,seq_len,64]
        score_embs_text, hidden_states_text = self.bert_encoder(sample_items_text)
        hidden_states_cv = [i[:,0] for i in hidden_states_cv]
        hidden_states_text = [i[:,0] for i in hidden_states_text]
        # hidden_states_cv or hidden_states_text's [-1] value is the last hidden states
        if self.args.remove_first == "TRUE":
            hidden_states_last_cv = hidden_states_cv[0]
            hidden_states_last_text = hidden_states_text[0]
            hidden_states_last_mm = torch.zeros(hidden_states_text[0].size()).to(hidden_states_text[0].device)

        else:
            hidden_states_last_cv = torch.zeros(hidden_states_cv[0].size()).to(hidden_states_cv[0].device)
            hidden_states_last_text = torch.zeros(hidden_states_text[0].size()).to(hidden_states_text[0].device)
            hidden_states_last_mm = torch.zeros(hidden_states_text[0].size()).to(hidden_states_text[0].device)

            
        # adding bert adapter here
        for index,_ in enumerate(self.side_bert_adapter_num_list):
            if "intra" in self.args.modality:
                if self.args.fusion_method =="gated":
                    side_gate_param = self.side_gate_params_cv[index]
                    gate = torch.sigmoid(side_gate_param / 0.1)
                    fusion_state_cv = gate * hidden_states_cv[self.side_cv_adapter_num_list[index]] + (1 - gate) * hidden_states_last_cv
                    side_gate_param = self.side_gate_params_text[index]
                    gate = torch.sigmoid(side_gate_param / 0.1)
                    fusion_state_text = gate * hidden_states_text[self.side_bert_adapter_num_list[index]] + (1 - gate) * hidden_states_last_text

                else:
                    fusion_state_cv = hidden_states_cv[self.side_cv_adapter_num_list[index]] + hidden_states_last_cv
                    fusion_state_text = hidden_states_text[self.side_bert_adapter_num_list[index]] + hidden_states_last_text
                hidden_states_last_text = self.bert_adapter_list[index](fusion_state_text)
                hidden_states_last_cv = self.cv_adapter_list[index](fusion_state_cv)

            # add three of them
            if "inter" in self.args.modality:
                if self.args.fusion_method =="gated":
                    side_gate_param = self.side_gate_params_mm[index]
                    gate = torch.sigmoid(side_gate_param / 0.1)
                    #hidden_states_last_mm =  hidden_states_last_mm + (1 - gate) * hidden_states_last_cv + gate * hidden_states_last_text 
                    hidden_states_last_mm = hidden_states_last_mm +  gate * hidden_states_cv[self.side_cv_adapter_num_list[index]] +  (1 - gate) *hidden_states_text[self.side_bert_adapter_num_list[index]]
                    hidden_states_last_mm = self.mm_adapter_list[index](hidden_states_last_mm)
                else:
                    hidden_states_last_mm= self.mm_adapter_list[index](hidden_states_last_mm+ hidden_states_cv[self.side_cv_adapter_num_list[index]]+hidden_states_text[self.side_bert_adapter_num_list[index]])
        if "intra" in self.args.modality:
            hidden_states_last_text = self.fc_bert(hidden_states_last_text)
            hidden_states_last_cv = self.fc_cv(hidden_states_last_cv)
        if "inter" in self.args.modality:
            hidden_states_last_mm = self.fc_mm(hidden_states_last_mm)
        
        # project the embedding down 
        if "intra" in self.args.modality:
            if "clip-vit" in self.args.CV_model_load:
                hidden_states_last_cv = self.cv_encoder.classifier(hidden_states_last_cv)
            elif "vit" in self.args.CV_model_load:
                hidden_states_last_cv = self.cv_encoder.image_net.classifier(hidden_states_last_cv)
            else:
                hidden_states_last_cv = self.cv_encoder.cv_proj(hidden_states_last_cv)
            hidden_states_last_text = self.bert_encoder.text_encoders.title.fc(hidden_states_last_text)
        if "inter" in self.args.modality:
            hidden_states_last_mm = self.fc_mm_down(hidden_states_last_mm)

        return hidden_states_last_cv, [hidden_states_last_text,hidden_states_last_mm]
